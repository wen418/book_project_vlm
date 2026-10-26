"""Experimental fixed-camera button colour rule. No ROS or model dependencies."""
import cv2
import numpy as np

DEFAULTS = {
    'search_roi': [190, 175, 135, 120], 'expected_size': [848, 480],
    'canny_low': 35, 'canny_high': 100, 'min_side': 22, 'max_side': 58,
    'hue_low_max': 12, 'hue_high_min': 170, 'min_saturation': 70,
    'min_value': 65, 'min_red_excess': 25,
    'on_ratio': 0.035, 'on_component_ratio': 0.012,
    'off_ratio': 0.006, 'off_component_ratio': 0.004,
    'max_white_ratio': 0.35, 'min_gray_std': 8,
    'outline_padding': 3, 'outline_method': 'gray_bgr_edges_with_and_without_closing_v2',
}


def locate_button(image, config):
    x, y, w, h = config['search_roi']
    if image.shape[1::-1] != tuple(config['expected_size']):
        return None, 'unexpected_image_size'
    search = image[y:y+h, x:x+w]
    contours = []
    for channel in [cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), *cv2.split(search)]:
        edges = cv2.Canny(cv2.GaussianBlur(channel, (3, 3), 0), config['canny_low'], config['canny_high'])
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        for edge_image in (edges, closed):
            contours.extend(cv2.findContours(edge_image, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0])
    candidates = []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, .035*perimeter, True)
        bx, by, bw, bh = cv2.boundingRect(contour)
        if not (4 <= len(polygon) <= 6 and config['min_side'] <= bw <= config['max_side']
                and config['min_side'] <= bh <= config['max_side'] and .72 <= bw/bh <= 1.4):
            continue
        fill = cv2.contourArea(contour)/(bw*bh)
        if fill < .65 or bx < 2 or by < 2 or bx+bw >= w-2 or by+bh >= h-2:
            continue
        # Geometry only: never select a button according to its colour or label.
        score = fill - .012*(abs(bw-40)+abs(bh-38)) - .004*(abs(bx+bw/2-43)+abs(by+bh/2-35))
        candidates.append((score, [x+bx, y+by, bw, bh]))
    if not candidates:
        return None, 'button_outline_not_found'
    bx, by, bw, bh = max(candidates, key=lambda pair: pair[0])[1]
    pad = config['outline_padding']
    left, top = max(x, bx-pad), max(y, by-pad)
    right, bottom = min(x+w, bx+bw+pad), min(y+h, by+bh+pad)
    return [left, top, right-left, bottom-top], ''


def analyze(image, config=None, precise=True):
    config = DEFAULTS if config is None else config
    roi, reason = locate_button(image, config)
    if roi is None:
        return {'prediction': None, 'reason': reason, 'roi': None}, None, None
    if not precise:
        roi = config['search_roi'][:]
    x, y, w, h = roi
    crop = image[y:y+h, x:x+w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    b, g, r = [part.astype(np.int16) for part in cv2.split(crop)]
    red = (((hue <= config['hue_low_max']) | (hue >= config['hue_high_min']))
           & (saturation >= config['min_saturation']) & (value >= config['min_value'])
           & (r-np.maximum(g, b) >= config['min_red_excess'])).astype(np.uint8)*255
    count, _, stats, _ = cv2.connectedComponentsWithStats(red, connectivity=8)
    largest = int(stats[1:, cv2.CC_STAT_AREA].max()) if count > 1 else 0
    ratio = float(np.count_nonzero(red)/red.size)
    component = largest/red.size
    white = float(np.mean((value >= 245) & (saturation <= 40)))
    gray_std = float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).std())
    if white > config['max_white_ratio'] or gray_std < config['min_gray_std']:
        prediction, reason = None, 'poor_exposure_or_contrast'
    elif ratio >= config['on_ratio'] and component >= config['on_component_ratio']:
        prediction, reason = True, 'red_emission_area'
    elif ratio <= config['off_ratio'] and component <= config['off_component_ratio']:
        prediction, reason = False, 'no_red_with_visible_button'
    else:
        prediction, reason = None, 'colour_boundary'
    return {'prediction': prediction, 'reason': reason, 'roi': roi, 'red_ratio': ratio,
            'largest_component_ratio': component, 'white_ratio': white, 'gray_std': gray_std}, crop, red
