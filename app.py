import os
import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify, send_from_directory
from scipy import signal, ndimage
from skimage import morphology
import math
from rembg import remove
from PIL import Image
import io
import json

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- PERSISTENCE SETUP ---
DB_FILE = 'specs_db.json'

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

# --- Calibration Constants ---
SCALE_AXIS = 122 / 3406.31   # mm/pixel (Length)
SCALE_PERP = 20.12 / 589.89  # mm/pixel (Diameter)

CAL_FILE = 'calibration.json'

def load_calibration():
    if not os.path.exists(CAL_FILE):
        return None
    try:
        with open(CAL_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

def save_calibration(scale_axis, scale_perp):
    with open(CAL_FILE, 'w') as f:
        json.dump({"SCALE_AXIS": scale_axis, "SCALE_PERP": scale_perp}, f, indent=4)

_cal = load_calibration()
if _cal:
    SCALE_AXIS = _cal["SCALE_AXIS"]
    SCALE_PERP = _cal["SCALE_PERP"]

def rotate_image(image, angle, center):
    rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
    h, w = image.shape[:2]
    return cv2.warpAffine(image, rot_mat, (w, h), flags=cv2.INTER_NEAREST)

def extract_profile_data(bw):
    rows, cols = np.nonzero(bw)
    if len(rows) == 0: return None, None, None

    min_y, max_y = np.min(rows), np.max(rows)
    margin = int((max_y - min_y) * 0.02) 
    start_y = min_y + margin
    end_y = max_y - margin
    
    widths = []
    right_profile = []
    valid_y = []

    for y in range(start_y, end_y):
        row_pixels = np.where(bw[y, :])[0]
        if len(row_pixels) > 0:
            l = row_pixels[0]
            r = row_pixels[-1]
            widths.append(r - l)
            right_profile.append(r)
            valid_y.append(y)
            
    return np.array(widths), np.array(right_profile), np.array(valid_y)

def analyze_geometry(widths, right_profile, valid_y):
    macro_shape = ndimage.median_filter(right_profile, size=51)
    micro_texture = right_profile - macro_shape
    
    local_roughness = ndimage.uniform_filter1d(np.abs(micro_texture), size=31)
    
    max_rough = np.percentile(local_roughness, 95)
    if max_rough < 0.5:
        return None, None, (0, len(widths) - 1)
        
    thread_mask = local_roughness > (max_rough * 0.4)
    thread_mask = ndimage.binary_closing(thread_mask, structure=np.ones(51))
    thread_mask = ndimage.binary_opening(thread_mask, structure=np.ones(51))
    
    if np.any(~thread_mask):
        baseline_width = np.median(widths[~thread_mask])
    else:
        baseline_width = np.median(widths)
        
    collar_mask = (widths > baseline_width * 1.04) & (~thread_mask)
    collar_mask = ndimage.binary_opening(collar_mask, structure=np.ones(21))
    
    smooth_mask = (~thread_mask) & (~collar_mask)
    smooth_mask = ndimage.binary_opening(smooth_mask, structure=np.ones(21))
    
    def get_largest_region(mask):
        labeled, num = ndimage.label(mask)
        if num == 0: return None
        max_len = 0
        best_idx = None
        for i in range(1, num + 1):
            idx = np.where(labeled == i)[0]
            if len(idx) > max_len:
                max_len = len(idx)
                best_idx = idx
        if max_len < 15: return None
        return (best_idx[0], best_idx[-1])
        
    return get_largest_region(thread_mask), get_largest_region(collar_mask), get_largest_region(smooth_mask)

def filter_indices_by_pitch(indices, y_coords, min_pitch_mm, max_pitch_mm):
    if len(indices) < 2: return indices
    y_vals = y_coords[indices]
    diffs_mm = np.diff(y_vals) * SCALE_AXIS
    valid_gap_mask = (diffs_mm >= min_pitch_mm) & (diffs_mm <= max_pitch_mm)
    if not np.any(valid_gap_mask): return []

    max_len = -1
    best_start = -1
    current_len = 0
    current_start = -1
    
    for i, is_valid in enumerate(valid_gap_mask):
        if is_valid:
            if current_len == 0: current_start = i
            current_len += 1
        else:
            if current_len > max_len:
                max_len = current_len
                best_start = current_start
            current_len = 0
    if current_len > max_len:
        max_len = current_len
        best_start = current_start
    if max_len == -1: return []

    return indices[best_start : best_start + max_len + 1]

def analyze_thread_signal(profile_x, profile_y):
    if len(profile_x) < 50: return None
    smooth_x = signal.savgol_filter(profile_x, 9, 2)
    window_size = 101
    if window_size > len(smooth_x): window_size = len(smooth_x) // 2 * 2 + 1
    trend = signal.savgol_filter(smooth_x, window_size, 2)
    normalized_signal = smooth_x - trend

    min_dist_pixels = int(1.0 / SCALE_AXIS) 
    search_distance = int(min_dist_pixels * 0.8)

    valleys_idx, _ = signal.find_peaks(-normalized_signal, prominence=0.5, distance=search_distance)
    peaks_idx, _ = signal.find_peaks(normalized_signal, prominence=0.5, distance=search_distance)

    valleys_idx = filter_indices_by_pitch(valleys_idx, profile_y, 0.8, 5.0)
    peaks_idx = filter_indices_by_pitch(peaks_idx, profile_y, 0.8, 5.0)

    if len(peaks_idx) < 2 or len(valleys_idx) < 2: return None
    return peaks_idx, valleys_idx, smooth_x

def fit_line_and_get_points(x_coords, y_coords):
    points = np.column_stack((x_coords, y_coords))
    line_params = cv2.fitLine(points, cv2.DIST_L2, 0, 0.01, 0.01)
    vx, vy, x0, y0 = line_params.flatten()
    min_y, max_y = np.min(y_coords), np.max(y_coords)
    def get_x(y):
        if abs(vy) < 1e-6: return x0 
        return x0 + (y - y0) * (vx / vy)
    p_start = [float(get_x(min_y)), float(min_y)]
    p_end = [float(get_x(max_y)), float(max_y)]
    return (vx, vy, x0, y0), [p_start, p_end]

def distance_between_lines(line1, line2):
    vx1, vy1, x1, y1 = line1
    vx2, vy2, x2, y2 = line2
    A = -vy1
    B = vx1
    C = vy1*x1 - vx1*y1
    dist = abs(A*x2 + B*y2 + C) / math.sqrt(A**2 + B**2)
    return float(dist)

def process_auto_metrics(image_path):
    img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
    if img is None: return None
    
    if len(img.shape) == 3 and img.shape[2] == 4:
        bw = img[:, :, 3]
        _, bw = cv2.threshold(bw, 127, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
    else:
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img.copy()

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
        gray_enhanced = clahe.apply(gray)
        blur = cv2.GaussianBlur(gray_enhanced, (7, 7), 0)
        _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        if np.mean(gray[bw > 0]) < np.mean(gray[bw == 0]): 
            bw = cv2.bitwise_not(bw)
            
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
    
    bw_bool = bw.astype(bool)
    bw_bool = ndimage.binary_fill_holes(bw_bool)
    bw_bool = morphology.remove_small_objects(bw_bool, 5000)
    bw = bw_bool.astype(np.uint8) * 255
    
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    viz_contour = []
    if contours:
        cnt = max(contours, key=cv2.contourArea)
        epsilon = 0.002 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        viz_contour = approx.reshape(-1, 2).tolist()
    
    y_idx, x_idx = np.nonzero(bw)
    if len(y_idx) == 0: return None
    
    coords = np.column_stack((x_idx, y_idx)).astype(float)
    mu = np.mean(coords, axis=0)
    cov_matrix = np.cov(coords, rowvar=False)
    eig_vals, eig_vecs = np.linalg.eigh(cov_matrix)
    sort_indices = np.argsort(eig_vals)[::-1]
    pc1 = eig_vecs[:, sort_indices[0]] 
    
    angle = np.arctan2(pc1[1], pc1[0]) * 180 / np.pi
    rotation_angle = 90 - angle
    
    cx, cy = int(mu[0]), int(mu[1])
    rot_bw = rotate_image(bw, rotation_angle, (cx, cy)) > 127

    widths, right_profile, valid_y = extract_profile_data(rot_bw)
    if widths is None: return None

    rot_y, rot_x = np.nonzero(rot_bw)
    min_ry, max_ry = np.min(rot_y), np.max(rot_y)
    min_rx, max_rx = np.min(rot_x), np.max(rot_x)
    
    box_rot = np.array([
        [min_rx, min_ry, 1],
        [max_rx, min_ry, 1],
        [max_rx, max_ry, 1],
        [min_rx, max_ry, 1]
    ])
    
    inv_R = cv2.getRotationMatrix2D((cx, cy), -rotation_angle, 1.0)
    box_orig = inv_R.dot(box_rot.T).T
    
    vis_bounding_box = []
    for p in box_orig:
        vis_bounding_box.append([float(p[0]), float(p[1])])

    def map_back_raw_lines(pts_list_homogenous):
        mapped = []
        for pt in pts_list_homogenous:
            orig = inv_R.dot(np.array(pt))
            mapped.append([float(orig[0]), float(orig[1])])
        return mapped

    thread_reg, collar_reg, smooth_reg = analyze_geometry(widths, right_profile, valid_y)

    if thread_reg is not None:
        start_idx, end_idx = thread_reg
    else:
        start_idx = int(len(valid_y) * 0.15)
        end_idx = int(len(valid_y) * 0.75)

    length_px = max_ry - min_ry
    
    thread_zone_widths = widths[start_idx:end_idx]
    if len(thread_zone_widths) > 10:
        diameter_px = np.percentile(thread_zone_widths, 95)
    else:
        diameter_px = np.percentile(widths, 95)
        
    thread_metrics = {}
    geometry_metrics = {}
    all_points = {}
    vis_extra_lines = []
    
    region_right_x = right_profile[start_idx:end_idx]
    region_y = valid_y[start_idx:end_idx]
    
    depth_px = 0
    core_dia_px = 0

    thread_data = analyze_thread_signal(region_right_x, region_y)
    
    if thread_data:
        peaks_idx, valleys_idx, smooth_x = thread_data
        
        TRIM = 3
        if len(peaks_idx) >= (TRIM * 2) + 2:
            peaks_idx = peaks_idx[TRIM:-TRIM]
        if len(valleys_idx) >= (TRIM * 2) + 2:
            valleys_idx = valleys_idx[TRIM:-TRIM]
        
        peaks_for_pitch = peaks_idx
        count = len(peaks_idx)
        
        if count == 3:
            peaks_for_pitch = peaks_idx[1:]
        elif count == 4:
            peaks_for_pitch = peaks_idx[1:3]
        elif count == 5:
            peaks_for_pitch = peaks_idx[1:4]
        elif count > 5:
            mid_idx = count // 2
            start_p = max(0, mid_idx - 2)
            peaks_for_pitch = peaks_idx[start_p : start_p + 5]

        peak_y_coords = region_y[peaks_for_pitch]
        if len(peak_y_coords) > 1:
            pitch_px = np.mean(np.diff(peak_y_coords))
        else:
            pitch_px = 0
            
        peak_x = smooth_x[peaks_idx]
        peak_y = region_y[peaks_idx]
        valley_x = smooth_x[valleys_idx]
        valley_y = region_y[valleys_idx]
        
        line_peaks, vis_line_p = fit_line_and_get_points(peak_x, peak_y)
        line_valleys, vis_line_v = fit_line_and_get_points(valley_x, valley_y)
        
        depth_px = distance_between_lines(line_peaks, line_valleys)
        
        pitch_mm = float(pitch_px * SCALE_AXIS)
        tpi = 25.4 / pitch_mm if pitch_mm > 0 else 0
        
        thread_metrics = {
            "pitch_mm": round(pitch_mm, 4),
            "pitch_tpi": round(tpi, 2),
            "depth_mm": round(float(depth_px * SCALE_PERP), 4),
            "count_peaks": int(len(peaks_idx)),
            "count_valleys": int(len(valleys_idx)),
            "count_peaks_used": int(len(peaks_for_pitch))
        }

        def map_back_points(indices, x_arr, y_arr, used_indices_set=None):
            mapped = []
            for i in indices:
                px, py = x_arr[i], y_arr[i]
                pt = np.array([px, py, 1])
                orig = inv_R.dot(pt)
                is_used = True
                if used_indices_set is not None:
                    is_used = (i in used_indices_set)
                mapped.append({
                    'x': float(orig[0]), 
                    'y': float(orig[1]), 
                    'is_used': is_used
                })
            return mapped

        def map_back_lines(pts_list):
            mapped = []
            for p in pts_list:
                pt = np.array([p[0], p[1], 1])
                orig = inv_R.dot(pt)
                mapped.append([float(orig[0]), float(orig[1])])
            return mapped
        
        used_peak_indices = set(peaks_for_pitch)

        all_points = {
            "peaks": map_back_points(peaks_idx, smooth_x, region_y, used_peak_indices),
            "valleys": map_back_points(valleys_idx, smooth_x, region_y),
            "line_peaks": map_back_lines(vis_line_p),
            "line_valleys": map_back_lines(vis_line_v)
        }
    
    core_dia_px = diameter_px - (2 * depth_px)
    
    def get_line_coords_for_width(target_width, search_widths, start_offset):
        idx = (np.abs(search_widths - target_width)).argmin()
        real_idx = start_offset + idx
        y_val = valid_y[real_idx]
        x_right = right_profile[real_idx]
        x_left = x_right - target_width
        return [[x_left, y_val, 1], [x_right, y_val, 1]]

    vis_outer_dia_line_rot = get_line_coords_for_width(diameter_px, thread_zone_widths, start_idx)
    vis_core_dia_line_rot = get_line_coords_for_width(core_dia_px, thread_zone_widths, start_idx)
    
    all_points["vis_outer_dia"] = map_back_raw_lines(vis_outer_dia_line_rot)
    all_points["vis_core_dia"] = map_back_raw_lines(vis_core_dia_line_rot)

    if collar_reg:
        cs, ce = collar_reg
        collar_widths = widths[cs:ce]
        max_idx_local = np.argmax(collar_widths)
        max_idx_global = cs + max_idx_local
        c_od_px = widths[max_idx_global]
        c_len_px = valid_y[ce] - valid_y[cs]
        
        geometry_metrics['collar'] = {
            'od_mm': round(float(c_od_px * SCALE_PERP), 2),
            'len_mm': round(float(c_len_px * SCALE_AXIS), 2)
        }
        
        y_val = valid_y[max_idx_global]
        x_r = right_profile[max_idx_global]
        x_l = x_r - c_od_px
        line_orig = map_back_raw_lines([[x_l, y_val, 1], [x_r, y_val, 1]])
        vis_extra_lines.append({
            "points": line_orig, "color": "#ff9f43", "label": "Collar OD", "val": geometry_metrics['collar']['od_mm']
        })

    if smooth_reg:
        ss, se = smooth_reg
        smooth_widths = widths[ss:se]
        base_dia_px = np.median(smooth_widths)
        base_len_px = valid_y[se] - valid_y[ss]
        
        geometry_metrics['base_dia'] = {
            'dia_mm': round(float(base_dia_px * SCALE_PERP), 2),
            'len_mm': round(float(base_len_px * SCALE_AXIS), 2)
        }
        
        mid_idx_local = len(smooth_widths) // 2
        mid_idx_global = ss + mid_idx_local
        y_val = valid_y[mid_idx_global]
        x_r = right_profile[mid_idx_global]
        x_l = x_r - base_dia_px
        line_orig = map_back_raw_lines([[x_l, y_val, 1], [x_r, y_val, 1]])
        vis_extra_lines.append({
            "points": line_orig, "color": "#2ecc71", "label": "Base Dia", "val": geometry_metrics['base_dia']['dia_mm']
        })
        
    all_points["vis_extra_lines"] = vis_extra_lines

    return {
        "length_mm": round(float(length_px * SCALE_AXIS), 2),
        "od_mm": round(float(diameter_px * SCALE_PERP), 2),
        "core_mm": round(float(core_dia_px * SCALE_PERP), 2),
        "img_width": int(img.shape[1]),
        "img_height": int(img.shape[0]),
        "thread_metrics": thread_metrics,
        "geometry_metrics": geometry_metrics, 
        "all_points": all_points,
        "vis_bounding_box": vis_bounding_box,
        "contour": viz_contour 
    }

# --- ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/upload', methods=['POST'])
def upload_image():
    if 'file' not in request.files: return jsonify({'error': 'No file'})
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'No selected file'})
    
    filename = file.filename
    if not filename.lower().endswith('.png'):
        filename = filename.rsplit('.', 1)[0] + '.png'
        
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as i:
            input_data = i.read()
            output_data = remove(input_data)
        with open(filepath, 'wb') as o:
            o.write(output_data)
    except Exception as e:
        print(f"Background removal failed: {e}")
    
    metrics = process_auto_metrics(filepath)
    if not metrics: return jsonify({'error': 'Processing failed'})
    
    return jsonify({'image_url': f'/uploads/{filename}', 'auto_data': metrics})

@app.route('/recalibrate', methods=['POST'])
def recalibrate():
    global SCALE_AXIS, SCALE_PERP

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'})

    file = request.files['file']
    known_length = request.form.get('known_length')
    known_diameter = request.form.get('known_diameter')

    if not known_length or not known_diameter:
        return jsonify({'error': 'Missing known dimensions'})

    try:
        known_length = float(known_length)
        known_diameter = float(known_diameter)
    except ValueError:
        return jsonify({'error': 'Invalid dimension values'})

    if known_length <= 0 or known_diameter <= 0:
        return jsonify({'error': 'Dimensions must be greater than zero'})

    filename = file.filename
    if not filename.lower().endswith('.png'):
        filename = filename.rsplit('.', 1)[0] + '.png'
        
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        with open(filepath, 'rb') as i:
            output_data = remove(i.read())
        with open(filepath, 'wb') as o:
            o.write(output_data)
    except Exception as e:
        print(f"Background removal failed: {e}")

    metrics = process_auto_metrics(filepath)
    if not metrics:
        return jsonify({'error': 'Could not process calibration image'})

    length_px = metrics['length_mm'] / SCALE_AXIS
    diameter_px = metrics['od_mm'] / SCALE_PERP

    if length_px <= 0 or diameter_px <= 0:
        return jsonify({'error': 'Could not detect valid dimensions in image'})

    SCALE_AXIS = known_length / length_px
    SCALE_PERP = known_diameter / diameter_px
    save_calibration(SCALE_AXIS, SCALE_PERP)

    return jsonify({
        'success': True,
        'new_scale_axis': round(SCALE_AXIS, 8),
        'new_scale_perp': round(SCALE_PERP, 8),
        'measured_length_mm': round(known_length, 4),
        'measured_diameter_mm': round(known_diameter, 4)
    })

@app.route('/save_spec', methods=['POST'])
def save_spec():
    data = request.json
    name = data.get('name')
    specs = data.get('specs')
    
    if not name or not specs:
        return jsonify({'error': 'Missing name or data'})
        
    db = load_db()
    db[name] = specs
    save_db(db)
    return jsonify({'success': True})

@app.route('/get_specs', methods=['GET'])
def get_specs():
    db = load_db()
    return jsonify(list(db.keys()))

@app.route('/get_spec/<n>', methods=['GET'])
def get_spec(n):
    db = load_db()
    if n in db:
        return jsonify(db[n])
    return jsonify({'error': 'Not found'}), 404

@app.route('/calculate_manual', methods=['POST'])
def calculate_manual():
    try:
        data = request.json
        points = data['points']
        if len(points) != 4: return jsonify({'error': 'Error: Need exactly 4 points'})
        
        p1, p2, v1, v2 = [np.array(p) for p in points]
        peak_pitch = np.linalg.norm(p2 - p1) * SCALE_AXIS
        valley_pitch = np.linalg.norm(v2 - v1) * SCALE_AXIS
        final_pitch = (peak_pitch + valley_pitch) / 2
        tpi = 25.4 / final_pitch if final_pitch > 0 else 0
        
        def get_line(pa, pb): return pb[1]-pa[1], pa[0]-pb[0], pb[0]*pa[1]-pa[0]*pb[1]
        A, B, C = get_line(p1, p2)
        if A == 0 and B == 0: return jsonify({'error': 'Points cannot be identical'})
        def dist(pt, A, B, C): return abs(A*pt[0] + B*pt[1] + C) / math.sqrt(A**2 + B**2)
        d1 = dist(v1, A, B, C)
        d2 = dist(v2, A, B, C)
        final_depth = ((d1 + d2) / 2) * SCALE_PERP
            
        return jsonify({
            'pitch_mm': round(float(final_pitch), 4),
            'pitch_tpi': round(float(tpi), 2), 
            'depth_mm': round(float(final_depth), 4)
        })
    except Exception as e:
        return jsonify({'error': 'Calculation Error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
