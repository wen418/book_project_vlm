import json
from pathlib import Path
import sys
import unittest
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dual_button import analyze_panel


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.layout = json.loads((ROOT/'button_layout.json').read_text(encoding='utf-8'))

    def image(self, upper=False, lower=False, count=2):
        image = np.full((480, 848, 3), 115, dtype=np.uint8)
        for i, lit in enumerate([upper, lower][:count]):
            x, y = 235, 200+65*i
            cv2.rectangle(image, (x, y), (x+34, y+32), (20, 20, 20), 2)
            cv2.arrowedLine(image, (x+17, y+25 if i == 0 else y+7),
                            (x+17, y+7 if i == 0 else y+25), (0, 0, 255) if lit else (45, 45, 45), 3, tipLength=.4)
        return image

    def test_independent_up_down_all_states(self):
        for up, down in [(True, False), (False, True), (True, True), (False, False)]:
            for direction, expected in [('up', up), ('down', down)]:
                with self.subTest(up=up, down=down, direction=direction):
                    result = analyze_panel(self.image(up, down), 19, direction, self.layout)
                    self.assertEqual(result['up_lit'], up, result)
                    self.assertEqual(result['down_lit'], down, result)
                    self.assertEqual(result['prediction'], expected)

    def test_only_one_visible_never_guess_in_dual_layout(self):
        result = analyze_panel(self.image(True, count=1), 19, 'down', self.layout)
        self.assertIsNone(result['prediction'])

    def test_wrong_resolution(self):
        result = analyze_panel(np.zeros((240, 424, 3), np.uint8), 19, 'up', self.layout)
        self.assertIsNone(result['prediction'])

    def test_single_floor_direction_not_available(self):
        result = analyze_panel(self.image(), 21, 'up', self.layout)
        self.assertIsNone(result['prediction'])
        self.assertEqual(result['reason'], 'requested_direction_not_available')

    def test_real_previously_verified_single_examples(self):
        inputs = ROOT.parents[1]/'Demo_vlm2_kuavo_api/results'
        examples = [('elevator_20261023_164433_137363_da5f03.png', False),
                    ('elevator_20261023_164456_298732_901f08.png', True),
                    ('elevator_20261025_174703_755449_56fde0.png', True)]
        available = [(inputs/n, expected) for n, expected in examples if (inputs/n).is_file()]
        if not available: self.skipTest('现场单按钮图片仅保存在本地，不属于发布文件')
        for path, expected in available:
            result = analyze_panel(cv2.imread(str(path)), 21, 'down', self.layout)
            self.assertEqual(result['down_lit'], expected, result)


if __name__ == '__main__': unittest.main()
