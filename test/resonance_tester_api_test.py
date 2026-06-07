#!/usr/bin/env python3
import os, sys, types, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
sys.modules['extras.bus'] = types.SimpleNamespace()

from extras import adxl345, resonance_tester


class FakeToolhead:
    def get_last_move_time(self):
        return 0.
    def manual_move(self, point, speed):
        pass
    def wait_moves(self):
        pass
    def dwell(self, delay):
        pass


class FakeAccelPrinter:
    def lookup_object(self, name):
        if name == 'toolhead':
            return FakeToolhead()
        raise KeyError(name)


class FakeGCode:
    def register_command(self, *args, **kwargs):
        pass


class FakeClientConnection:
    def __init__(self):
        self.sent = []
        self.closed = False
    def send(self, msg):
        self.sent.append(msg)
    def is_closed(self):
        return self.closed


class FakeWebRequestError(Exception):
    pass


class FakeWebRequest:
    error = FakeWebRequestError
    def __init__(self, params=None, client=None):
        self.params = params or {}
        self.client = client or FakeClientConnection()
        self.response = None
    def get_client_connection(self):
        return self.client
    def get(self, item, default=None):
        return self.params.get(item, default)
    def get_dict(self, item, default=None):
        return self.get(item, default)
    def get_str(self, item, default=None):
        return self.get(item, default)
    def send(self, data):
        self.response = data


class FakeWebhooks:
    def __init__(self):
        self.endpoints = {}
    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback


class FakePrinter:
    command_error = FakeWebRequestError
    config_error = FakeWebRequestError
    def __init__(self):
        self.gcode = FakeGCode()
        self.webhooks = FakeWebhooks()
        self.toolhead = FakeToolhead()
    def lookup_object(self, name):
        if name == 'gcode':
            return self.gcode
        if name == 'webhooks':
            return self.webhooks
        if name == 'toolhead':
            return self.toolhead
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


class FakeShaperCalibrate:
    def __init__(self, csv_data):
        self.csv_data = csv_data
        self.calls = []
    def write_calibration_data(self, calibration_data, shapers=None,
                               max_freq=None):
        self.calls.append((calibration_data, shapers, max_freq))
        return self.csv_data


class FakeGenerator:
    def prepare_test(self, gcmd, is_z):
        pass
    def gen_test(self):
        return []


class FakeExecutor:
    def run_test(self, test_seq, axis, gcmd):
        pass


class FakeAccelClient:
    def finish_measurements(self):
        pass


class FakeChip:
    name = 'adxl345'
    def __init__(self):
        self.calls = []
    def start_internal_client(self, batch_cb=None, store_samples=True):
        self.calls.append((batch_cb, store_samples))
        return FakeAccelClient()


class ResonanceTesterAPITest(unittest.TestCase):
    def make_tester(self):
        config = FakeConfig()
        return resonance_tester.ResonanceTester(config)

    def test_accel_query_helper_publishes_raw_batches(self):
        published = []
        helper = adxl345.AccelQueryHelper(
            FakeAccelPrinter(), batch_cb=published.append)
        msg = {'data': [(1., 2., 3., 4.)], 'errors': 0, 'overflows': 0}

        self.assertTrue(helper.handle_batch(msg))

        self.assertEqual(published, [msg])

    def test_accel_query_helper_can_stream_without_buffering_samples(self):
        published = []
        helper = adxl345.AccelQueryHelper(
            FakeAccelPrinter(), batch_cb=published.append,
            store_samples=False)
        msg = {'data': [(1., 2., 3., 4.)], 'errors': 0, 'overflows': 0}

        self.assertTrue(helper.handle_batch(msg))

        self.assertEqual(published, [msg])
        self.assertEqual(helper.msgs, [])

    def test_processed_results_are_stored_for_api_retrieval(self):
        tester = self.make_tester()
        helper = FakeShaperCalibrate("freq,psd_x\n1.0,2.0\n")
        calibration_data = object()

        result_id = tester.save_calibration_data(
            'resonances', 'run', helper, resonance_tester.TestAxis('x'),
            calibration_data, max_freq=200.)

        list_request = FakeWebRequest()
        tester._handle_list_results(list_request)
        get_request = FakeWebRequest({'id': result_id})
        tester._handle_get_result(get_request)

        self.assertEqual(helper.calls, [(calibration_data, None, 200.)])
        self.assertEqual(list_request.response['results'][0]['id'], result_id)
        self.assertNotIn('data', list_request.response['results'][0])
        self.assertEqual(get_request.response['data'], "freq,psd_x\n1.0,2.0\n")
        self.assertEqual(get_request.response['content_type'], 'text/csv')

    def test_raw_data_is_published_to_subscribed_api_clients(self):
        tester = self.make_tester()
        client = FakeClientConnection()
        request = FakeWebRequest(
            {'response_template': {'id': 42}}, client=client)

        tester._handle_subscribe_raw_data(request)
        tester._publish_raw_data(
            'run-1', 'x', [20., 20., 20.], 'adxl345',
            {'data': [(1., 2., 3., 4.)], 'errors': 0, 'overflows': 0})

        self.assertEqual(
            request.response,
            {'header': ('time', 'x_acceleration',
                        'y_acceleration', 'z_acceleration')})
        self.assertEqual(client.sent[0]['id'], 42)
        self.assertEqual(client.sent[0]['params']['run_id'], 'run-1')
        self.assertEqual(client.sent[0]['params']['axis'], 'x')
        self.assertEqual(client.sent[0]['params']['point'], [20., 20., 20.])
        self.assertEqual(client.sent[0]['params']['chip_name'], 'adxl345')
        self.assertEqual(
            client.sent[0]['params']['data'], [(1., 2., 3., 4.)])

    def test_raw_only_resonance_run_streams_without_buffering_samples(self):
        tester = self.make_tester()
        tester.generator = FakeGenerator()
        tester.executor = FakeExecutor()
        chip = FakeChip()
        tester.accel_chips = [('x', chip)]

        tester._run_test(
            FakeWebRequest(), [resonance_tester.TestAxis('x')], None,
            'run', raw_output=True, run_id='run-1')

        self.assertEqual(len(chip.calls), 1)
        self.assertIsNotNone(chip.calls[0][0])
        self.assertFalse(chip.calls[0][1])


if __name__ == '__main__':
    unittest.main()
