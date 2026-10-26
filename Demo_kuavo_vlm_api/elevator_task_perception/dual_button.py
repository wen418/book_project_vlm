"""Find independent button outlines, then apply the unchanged red-light rule."""
import cv2
from button_color_rule import DEFAULTS, analyze


def validate_layout(layout):
    if layout.get('dual_order') != ['up', 'down']:
        raise ValueError('仅支持已标定的上键在上、下键在下的竖直布局')
    for key in ('dual_search_roi', 'single_search_roi'):
        roi = layout.get(key)
        if (not isinstance(roi, list) or len(roi) != 4 or not all(type(v) is int for v in roi)
                or min(roi[:2]) < 0 or min(roi[2:]) <= 0 or roi[0]+roi[2] > 848 or roi[1]+roi[3] > 480):
            raise ValueError('按钮搜索 ROI 无效：'+key)
    single = layout.get('single_button_floors')
    if not isinstance(single, dict) or any(v not in ('up', 'down') for v in single.values()):
        raise ValueError('单按钮楼层配置无效')
    return layout


def candidates(image, config):
    x, y, w, h = config['search_roi']
    if image.shape[1::-1] != tuple(config['expected_size']):
        return []
    search = image[y:y+h, x:x+w]
    contours = []
    for channel in [cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), *cv2.split(search)]:
        edges = cv2.Canny(cv2.GaussianBlur(channel, (3, 3), 0), 35, 100)
        for edge in (edges, cv2.morphologyEx(edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))):
            contours.extend(cv2.findContours(edge, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0])
    found = []
    for contour in contours:
        bx, by, bw, bh = cv2.boundingRect(contour)
        poly = cv2.approxPolyDP(contour, .035*cv2.arcLength(contour, True), True)
        if not (4 <= len(poly) <= 6 and 22 <= bw <= 58 and 22 <= bh <= 58
                and .72 <= bw/bh <= 1.4 and 2 <= bx and 2 <= by and bx+bw < w-2 and by+bh < h-2):
            continue
        fill = cv2.contourArea(contour)/(bw*bh)
        if fill >= .65:
            found.append((fill-.012*(abs(bw-40)+abs(bh-38)), [x+bx, y+by, bw, bh]))
    unique = []
    for score, box in sorted(found, reverse=True):
        cx, cy = box[0]+box[2]/2, box[1]+box[3]/2
        if not any(abs(cx-(b[0]+b[2]/2)) < .4*max(b[2], box[2]) and
                   abs(cy-(b[1]+b[3]/2)) < .4*max(b[3], box[3]) for _, b in unique):
            unique.append((score, box))
    return unique


def analyze_panel(image, current_floor, direction, layout):
    result = {'up_lit': None, 'down_lit': None, 'selected_direction': direction,
              'prediction': None, 'reason': 'button_pair_not_found', 'buttons': {}}
    if direction not in ('up', 'down'):
        result['reason'] = 'missing_button_direction'
        return result
    single = layout['single_button_floors'].get(str(current_floor))
    if single:
        if single != direction:
            result['reason'] = 'requested_direction_not_available'
            return result
        feature, _, _ = analyze(image, {**DEFAULTS, 'search_roi': layout['single_search_roi']})
        result.update({single+'_lit': feature['prediction'], 'prediction': feature['prediction'],
                       'reason': feature['reason'], 'buttons': {single: feature}, 'layout': 'configured_single'})
        return result
    boxes = candidates(image, {**DEFAULTS, 'search_roi': layout['dual_search_roi']})
    pairs = []
    for i, (sa, a) in enumerate(boxes):
        for sb, b in boxes[i+1:]:
            top, bottom = sorted((a, b), key=lambda r: r[1]+r[3]/2)
            dx = abs(top[0]+top[2]/2-bottom[0]-bottom[2]/2)
            dy = bottom[1]+bottom[3]/2-top[1]-top[3]/2
            if (dx <= .45*max(top[2], bottom[2]) and
                    .9*max(top[3], bottom[3]) <= dy <= 3.5*max(top[3], bottom[3]) and
                    min(top[2], bottom[2])/max(top[2], bottom[2]) >= .65):
                pairs.append((sa+sb-.01*dx, top, bottom))
    pairs.sort(key=lambda p: p[0], reverse=True)
    if not pairs or (len(pairs) > 1 and pairs[0][0]-pairs[1][0] < .12):
        result['reason'] = 'button_pair_ambiguous' if pairs else 'button_pair_not_found'
        return result
    for name, box in zip(layout['dual_order'], pairs[0][1:]):
        x, y, w, h = box
        # A small local search prevents the second button from replacing this one.
        local = [max(0, x-6), max(0, y-6), w+12, h+12]
        local[2] = min(local[2], image.shape[1]-local[0])
        local[3] = min(local[3], image.shape[0]-local[1])
        feature, _, _ = analyze(image, {**DEFAULTS, 'search_roi': local})
        result[name+'_lit'] = feature['prediction']
        result['buttons'][name] = feature
    result['prediction'] = result[direction+'_lit']
    result['reason'] = result['buttons'][direction]['reason']
    result['layout'] = 'configured_vertical_pair'
    return result
