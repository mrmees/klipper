#!/usr/bin/env python3
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from extras import resonance_tester
import calibrate_shaper


class FakeGCode:
    def register_command(self, *args, **kwargs):
        pass
    def get_mutex(self):
        return self
    def __enter__(self):
        pass
    def __exit__(self, type=None, value=None, tb=None):
        pass


class FakeWebhooks:
    def __init__(self):
        self.endpoints = {}
    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback


class FakeWebRequestError(Exception):
    pass


class FakeWebRequest:
    error = FakeWebRequestError
    def __init__(self, params=None):
        self.params = params or {}
        self.response = None
    def get(self, item, default=None, types=None):
        return self.params.get(item, default)
    def get_str(self, item, default=None):
        return self.get(item, default)
    def get_int(self, item, default=None):
        return self.get(item, default)
    def get_float(self, item, default=None):
        value = self.get(item, default)
        return None if value is None else float(value)
    def get_dict(self, item, default=None):
        return self.get(item, default)
    def send(self, data):
        self.response = data


class FakeToolhead:
    def __init__(self):
        self.last_move_time = 10.
        self.moves = []
        self.wait_count = 0
        self.dwell_times = []
    def get_last_move_time(self):
        curtime = self.last_move_time
        self.last_move_time += 10.
        return curtime
    def manual_move(self, point, speed):
        self.moves.append((point, speed))
    def wait_moves(self):
        self.wait_count += 1
    def dwell(self, delay):
        self.dwell_times.append(delay)


class FakePrinter:
    command_error = FakeWebRequestError
    config_error = FakeWebRequestError
    def __init__(self):
        self.gcode = FakeGCode()
        self.webhooks = FakeWebhooks()
        self.toolhead = FakeToolhead()
    def lookup_object(self, name, default=None):
        if name == 'gcode':
            return self.gcode
        if name == 'webhooks':
            return self.webhooks
        if name == 'toolhead':
            return self.toolhead
        if default is not None:
            return default
        raise KeyError(name)
    def register_event_handler(self, *args):
        pass


class FakeConfig:
    error = FakeWebRequestError
    def __init__(self):
        self.printer = FakePrinter()
    def get_printer(self):
        return self.printer
    def get(self, name, default=None):
        values = {
            'accel_chip': 'adxl345',
            'accel_chip_x': None,
            'accel_chip_z': '',
        }
        return values.get(name, default)
    def getfloat(self, name, default=None, **kwargs):
        return default
    def getlists(self, name, **kwargs):
        return [[20., 20., 20.]]


class FakeGenerator:
    def __init__(self):
        self.prepared = []
    def prepare_test(self, gcmd, is_z):
        self.prepared.append(is_z)
    def gen_test(self):
        return [(0.1, 1., 5.)]
    def get_max_freq(self):
        return 135.


class FakeExecutor:
    def __init__(self):
        self.runs = []
    def run_test(self, test_seq, axis, gcmd):
        self.runs.append((test_seq, axis.get_name()))


class FakeChip:
    name = 'adxl345'
    api_dump_endpoint = 'adxl345/dump_adxl345'
    def __init__(self):
        self.calls = []
    def start_internal_client(self):
        self.calls.append('start_internal_client')


class ResonanceTesterAPITest(unittest.TestCase):
    def make_tester(self):
        config = FakeConfig()
        return resonance_tester.ResonanceTester(config)

    def test_registers_run_test_api_endpoint(self):
        tester = self.make_tester()

        self.assertIn(
            'resonance_tester/run_test',
            tester.printer.webhooks.endpoints)

    def test_run_test_api_reports_capture_windows_without_internal_capture(self):
        tester = self.make_tester()
        tester.generator = FakeGenerator()
        tester.executor = FakeExecutor()
        chip = FakeChip()
        tester.accel_chips = [('xy', chip)]

        request = FakeWebRequest({'axis': 'x'})
        tester._handle_run_test(request)

        self.assertEqual(chip.calls, [])
        self.assertEqual(tester.executor.runs[0][1], 'x')
        self.assertEqual(
            request.response,
            {'capture_windows': [{
                'axis': 'x',
                'axis_direction': [1., 0., 0.],
                'point': [20., 20., 20.],
                'chip_axis': 'xy',
                'sensor': 'adxl345',
                'api_method': 'adxl345/dump_adxl345',
                'api_params': {'sensor': 'adxl345'},
                'start_time': 10.,
                'end_time': 20.,
                'max_freq': 202.5,
            }]})


class CalibrateShaperAPITest(unittest.TestCase):
    def test_api_sample_buffer_filters_streamed_batches_by_time_window(self):
        samples = calibrate_shaper.ApiSampleBuffer(
            'adxl345/dump_adxl345', 'adxl345')

        samples.add_batch({
            'data': [
                [0.10, 1., 2., 3.],
                [0.20, 4., 5., 6.],
            ]})
        samples.add_batch({
            'data': [
                [0.30, 7., 8., 9.],
                [0.40, 10., 11., 12.],
            ]})

        data = samples.get_window_data(0.20, 0.30)

        self.assertEqual(data.shape, (2, 4))
        self.assertEqual(data.tolist(), [
            [0.20, 4., 5., 6.],
            [0.30, 7., 8., 9.],
        ])

    def test_api_subscription_discovery_uses_existing_dump_endpoints(self):
        settings = {
            'resonance_tester': {'accel_chip': 'adxl345'},
            'adxl345': {},
        }

        subscriptions = calibrate_shaper.find_api_subscriptions(
            settings, 'x', None)

        self.assertEqual(subscriptions, [{
            'chip': 'adxl345',
            'sensor': 'adxl345',
            'method': 'adxl345/dump_adxl345',
            'params': {'sensor': 'adxl345'},
        }])

    def test_api_subscription_discovery_matches_custom_axis_to_axis_chips(self):
        settings = {
            'resonance_tester': {
                'accel_chip_x': 'adxl345 hotend',
                'accel_chip_y': 'lis2dw bed',
                'accel_chip_z': '',
            },
            'adxl345 hotend': {},
            'lis2dw bed': {},
        }

        subscriptions = calibrate_shaper.find_api_subscriptions(
            settings, '1,1', None)

        self.assertEqual(subscriptions, [{
            'chip': 'adxl345 hotend',
            'sensor': 'hotend',
            'method': 'adxl345/dump_adxl345',
            'params': {'sensor': 'hotend'},
        }, {
            'chip': 'lis2dw bed',
            'sensor': 'bed',
            'method': 'lis2dw/dump_lis2dw',
            'params': {'sensor': 'bed'},
        }])


if __name__ == '__main__':
    unittest.main()
