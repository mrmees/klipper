#!/usr/bin/env python3
# Shaper auto-calibration script
#
# Copyright (C) 2020-2025  Dmitry Butyugin <dmbutyugin@google.com>
# Copyright (C) 2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import print_function
import csv, errno, importlib, json, optparse, os, re, select, socket, sys, time
from textwrap import wrap
import numpy as np, matplotlib
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             '..', 'klippy'))
shaper_calibrate = importlib.import_module('.shaper_calibrate', 'extras')

MAX_TITLE_LENGTH=65
API_TERMINATOR = b'\x03'
ClientInfo = {'program': 'calibrate_shaper', 'version': 'v0.1'}
ConfigSubscriptions = {
    'adxl345': 'adxl345/dump_adxl345',
    'bmi160': 'bmi160/dump_bmi160',
    'icm20948': 'icm20948/dump_icm20948',
    'lis2dw': 'lis2dw/dump_lis2dw',
    'lis3dh': 'lis2dw/dump_lis2dw',
    'mpu9250': 'mpu9250/dump_mpu9250',
}

class ApiSampleBuffer:
    def __init__(self, method, sensor):
        self.method = method
        self.sensor = sensor
        self.chunks = []
        self.errors = 0
        self.overflows = 0
    def add_batch(self, params):
        self.errors += params.get('errors', 0)
        self.overflows += params.get('overflows', 0)
        data = params.get('data')
        if not data:
            return
        self.chunks.append(np.asarray(data, dtype=np.float64))
    def get_window_data(self, start_time, end_time):
        chunks = []
        for chunk in self.chunks:
            times = chunk[:, 0]
            mask = (times >= start_time) & (times <= end_time)
            if mask.any():
                chunks.append(chunk[mask])
        if not chunks:
            return np.zeros((0, 4), dtype=np.float64)
        return np.concatenate(chunks)

class KlipperApiClient:
    def __init__(self, uds_filename):
        self.uds_filename = uds_filename
        self.sock = None
        self.socket_data = b""
        self.next_query_id = 1
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(1)
        while 1:
            try:
                self.sock.connect(self.uds_filename)
            except socket.error as e:
                if e.errno == errno.ECONNREFUSED:
                    time.sleep(0.1)
                    continue
                raise
            break
    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
    def send_query(self, method, params=None):
        msg_id = self.next_query_id
        self.next_query_id += 1
        msg = {'id': msg_id, 'method': method, 'params': params or {}}
        raw = json.dumps(msg, separators=(',', ':')).encode()
        self.sock.sendall(raw + API_TERMINATOR)
        return msg_id
    def _read_message(self):
        while API_TERMINATOR not in self.socket_data:
            select.select([self.sock], [], [])
            data = self.sock.recv(4096)
            if not data:
                raise IOError("Klipper API socket closed")
            self.socket_data += data
        raw, self.socket_data = self.socket_data.split(API_TERMINATOR, 1)
        return json.loads(raw.decode())
    def query(self, method, params=None, sample_buffers=None):
        msg_id = self.send_query(method, params)
        while 1:
            msg = self._read_message()
            qid = msg.get('q')
            if qid is not None and sample_buffers is not None:
                sample_buffer = sample_buffers.get(qid)
                if sample_buffer is not None:
                    sample_buffer.add_batch(msg.get('params', {}))
                continue
            if msg.get('id') != msg_id:
                continue
            if 'error' in msg:
                raise Exception(msg['error'].get('message', msg['error']))
            return msg.get('result', {})

def _split_csv(value):
    if not value:
        return []
    if type(value) in (list, tuple):
        return [v.strip() for v in value if v.strip()]
    return [v.strip() for v in value.split(',') if v.strip()]

def _sensor_name(chip_name):
    return chip_name.split()[-1]

def _chip_type(chip_name):
    return chip_name.split()[0]

def _chip_subscription(chip_name):
    chip_type = _chip_type(chip_name)
    method = ConfigSubscriptions.get(chip_type)
    if method is None:
        raise ValueError("Unsupported accelerometer type '%s'" % (chip_type,))
    sensor = _sensor_name(chip_name)
    return {
        'chip': chip_name,
        'sensor': sensor,
        'method': method,
        'params': {'sensor': sensor},
    }

def _axis_dirs(axis):
    axis = axis.lower()
    if axis == 'x':
        return (1., 0., 0.)
    if axis == 'y':
        return (0., 1., 0.)
    if axis == 'z':
        return (0., 0., 1.)
    dirs = [float(v.strip()) for v in axis.split(',')]
    if len(dirs) == 2:
        dirs.append(0.)
    if len(dirs) != 3:
        raise ValueError("Invalid axis '%s'" % (axis,))
    return tuple(dirs)

def find_api_subscriptions(settings, axis, chips):
    chip_names = _split_csv(chips)
    if not chip_names:
        rconfig = settings.get('resonance_tester', {})
        axis = axis.lower()
        if rconfig.get('accel_chip_x'):
            if axis == 'x':
                chip_names = _split_csv(rconfig.get('accel_chip_x'))
            elif axis == 'y':
                chip_names = _split_csv(rconfig.get('accel_chip_y'))
            elif axis == 'z':
                chip_names = _split_csv(rconfig.get('accel_chip_z'))
            else:
                dirs = _axis_dirs(axis)
                if dirs[0]:
                    chip_names.extend(_split_csv(rconfig.get('accel_chip_x')))
                if dirs[1]:
                    chip_names.extend(_split_csv(rconfig.get('accel_chip_y')))
                if dirs[2]:
                    chip_names.extend(_split_csv(rconfig.get('accel_chip_z')))
        elif axis == 'z':
            chip_names = _split_csv(rconfig.get('accel_chip_z'))
        else:
            chip_names = _split_csv(rconfig.get('accel_chip'))
    subscriptions = []
    seen = set()
    for chip_name in chip_names:
        sub = _chip_subscription(chip_name)
        key = (sub['method'], sub['sensor'])
        if key not in seen:
            seen.add(key)
            subscriptions.append(sub)
    return subscriptions

def parse_log(logname):
    with open(logname) as f:
        for header in f:
            if not header.startswith('#'):
                break
        if not header.startswith('freq,'):
            # Process raw accelerometer data
            data = np.loadtxt(logname, comments='#', delimiter=',')
            helper = shaper_calibrate.ShaperCalibrate(printer=None)
            calibration_data = helper.process_accelerometer_data(logname, data)
            calibration_data.normalize_to_frequencies()
            return calibration_data
    # Parse power spectral density data
    data = np.genfromtxt(logname, dtype=np.float64, skip_header=1,
                         comments='#', delimiter=',', filling_values=0.)
    if header.startswith('freq,psd_x,psd_y,psd_z,psd_xyz'):
        calibration_data = shaper_calibrate.CalibrationData(
                name=logname, freq_bins=data[:,0], psd_sum=data[:,4],
                psd_x=data[:,1], psd_y=data[:,2], psd_z=data[:,3])
        calibration_data.set_numpy(np)
    else:
        parsed_header = next(csv.reader([header], delimiter=','))
        calibration_data = None
        for i, dataset_name in enumerate(parsed_header[1:]):
            if dataset_name == 'shapers:':
                break
            cdata = shaper_calibrate.CalibrationData(
                    name=dataset_name, freq_bins=data[:,0], psd_sum=data[:,i+1],
                    # Individual per-axis data is not stored
                    psd_x=None, psd_y=None, psd_z=None)
            cdata.set_numpy(np)
            if calibration_data is None:
                calibration_data = cdata
            else:
                calibration_data.add_data(cdata)
    # If input shapers are present in the CSV file, the frequency
    # response is already normalized to input frequencies
    if ',shapers:' not in header:
        calibration_data.normalize_to_frequencies()
    return calibration_data

######################################################################
# Shaper calibration
######################################################################

# Find the best shaper parameters
def calibrate_shaper(datas, csv_output, *, shapers, damping_ratio, scv,
                     shaper_freqs, max_smoothing, max_vibrs_pcnt,
                     test_damping_ratios, max_freq):
    # Combine accelerometer data
    calibration_data = datas[0]
    for data in datas[1:]:
        calibration_data.add_data(data)
    max_vibrations = None if max_vibrs_pcnt is None else max_vibrs_pcnt * 0.01

    print("Processing resonances from %s"
          % ",".join(d.name for d in calibration_data.get_datasets()))
    helper = shaper_calibrate.ShaperCalibrate(printer=None)
    shaper, all_shapers = helper.find_best_shaper(
            calibration_data, shapers=shapers, damping_ratio=damping_ratio,
            scv=scv, shaper_freqs=shaper_freqs, max_smoothing=max_smoothing,
            max_vibrations=max_vibrations,
            test_damping_ratios=test_damping_ratios, max_freq=max_freq,
            logger=print)
    if not shaper:
        print("No recommended shaper, possibly invalid value for --shapers=%s" %
              (','.join(shapers)))
        return None, None, None
    print("Recommended shaper is %s @ %.1f Hz" % (shaper.name, shaper.freq))
    if csv_output is not None:
        helper.save_calibration_data(
                csv_output, calibration_data, all_shapers)
    return shaper.name, all_shapers, calibration_data

######################################################################
# Plot frequency response and suggested input shapers
######################################################################

def plot_freq_response(calibration_data, shapers,
                       selected_shaper, max_freq):
    selected_shaper_data = [s for s in shapers if s.name == selected_shaper][0]
    max_freq_bin = selected_shaper_data.freq_bins.max()
    if max_freq > max_freq_bin:
        max_freq = max_freq_bin

    fontP = matplotlib.font_manager.FontProperties()
    fontP.set_size('x-small')

    fig, ax = matplotlib.pyplot.subplots(figsize=(8, 5))
    ax.set_xlabel('Frequency, Hz')
    ax.set_xlim([0, max_freq])
    ax.set_ylabel('Power spectral density')

    datasets = calibration_data.get_datasets()
    if len(datasets) == 1:
        freqs = calibration_data.freq_bins
        psd = calibration_data.psd_sum[freqs <= max_freq]
        px = calibration_data.psd_x[freqs <= max_freq]
        py = calibration_data.psd_y[freqs <= max_freq]
        pz = calibration_data.psd_z[freqs <= max_freq]
        freqs = freqs[freqs <= max_freq]
        after_shaper = np.interp(selected_shaper_data.freq_bins, freqs, psd)
        ax.plot(freqs, psd, label='X+Y+Z', color='purple')
        ax.plot(freqs, px, label='X', color='red')
        ax.plot(freqs, py, label='Y', color='green')
        ax.plot(freqs, pz, label='Z', color='blue')
        title = "Frequency response and shapers (%s)" % calibration_data.name
    else:
        after_shaper = np.zeros(shape=selected_shaper_data.freq_bins.shape)
        for data in datasets:
            freqs = data.freq_bins
            psd = data.psd_sum[freqs <= max_freq]
            freqs = freqs[freqs <= max_freq]
            after_shaper = np.maximum(
                    after_shaper, np.interp(selected_shaper_data.freq_bins,
                                            freqs, psd))
            ax.plot(freqs, psd, label=data.name)
            title = "Frequency responses and shapers"
    after_shaper *= selected_shaper_data.vals

    ax.set_title("\n".join(wrap(title, MAX_TITLE_LENGTH)))
    ax.xaxis.set_minor_locator(matplotlib.ticker.MultipleLocator(5))
    ax.yaxis.set_minor_locator(matplotlib.ticker.AutoMinorLocator())
    ax.ticklabel_format(axis='y', style='scientific', scilimits=(0,0))
    ax.grid(which='major', color='grey')
    ax.grid(which='minor', color='lightgrey')

    ax2 = ax.twinx()
    ax2.set_ylabel('Shaper vibration reduction (ratio)')
    best_shaper_vals = None
    for shaper in shapers:
        label = "%s (%.1f Hz, vibr=%.1f%%, sm~=%.2f, accel<=%.f)" % (
                shaper.name.upper(), shaper.freq,
                shaper.vibrs * 100., shaper.smoothing,
                round(shaper.max_accel / 100.) * 100.)
        linestyle = 'dotted'
        if shaper.name == selected_shaper:
            linestyle = 'dashdot'
        ax2.plot(shaper.freq_bins, shaper.vals,
                 label=label, linestyle=linestyle)
    ax.plot(selected_shaper_data.freq_bins, after_shaper,
            label='After%sshaper' % ('\n' if len(datasets) == 1 else ' '),
            color='cyan')
    # A hack to add a human-readable shaper recommendation to legend
    ax2.plot([], [], ' ',
             label="Recommended shaper: %s" % (selected_shaper.upper()))

    ax.legend(loc='upper left', prop=fontP)
    ax2.legend(loc='upper right', prop=fontP)

    fig.tight_layout()
    return fig

######################################################################
# API capture
######################################################################

def _get_api_config(api_client):
    api_client.query("info", {"client_info": ClientInfo})
    result = api_client.query("objects/query", {
        "objects": {
            "configfile": ["settings"],
            "toolhead": ["square_corner_velocity"],
        }})
    status = result.get('status', {})
    return status.get('configfile', {}).get('settings', {}), status

def _subscription_key(sub):
    return "%s:%s" % (sub['method'], sub['sensor'])

def _run_test_params(options):
    params = {'axis': options.axis}
    if options.chips:
        params['chips'] = options.chips
    if options.point:
        params['point'] = options.point
    api_opts = [
        ('name', 'name'),
        ('freq_start', 'freq_start'),
        ('freq_end', 'freq_end'),
        ('accel_per_hz', 'accel_per_hz'),
        ('hz_per_sec', 'hz_per_sec'),
        ('sweeping_accel', 'sweeping_accel'),
        ('sweeping_period', 'sweeping_period'),
        ('input_shaping', 'input_shaping'),
    ]
    for opt_name, api_name in api_opts:
        value = getattr(options, opt_name)
        if value is not None:
            params[api_name] = value
    return params

def _process_api_windows(windows, sample_buffers):
    helper = shaper_calibrate.ShaperCalibrate(printer=None)
    datas = []
    max_freq = 0.
    if not windows:
        raise Exception("No resonance capture windows returned")
    for i, window in enumerate(windows):
        key = "%s:%s" % (window['api_method'], window['sensor'])
        sample_buffer = sample_buffers.get(key)
        if sample_buffer is None:
            raise Exception("No subscription for %s" % (key,))
        samples = sample_buffer.get_window_data(
                window['start_time'], window['end_time'])
        if not samples.size:
            raise Exception("No samples captured for %s axis from %s" % (
                            window['axis'], window['sensor']))
        name = "%s_%s_%d" % (window['axis'], window['sensor'], i + 1)
        cdata = helper.process_accelerometer_data(name, samples)
        cdata.normalize_to_frequencies()
        datas.append(cdata)
        max_freq = max(max_freq, window.get('max_freq') or 0.)
    return datas, max_freq or None

def capture_api_data(options, opts):
    if not options.axis:
        opts.error("--api requires --axis")
    api_client = KlipperApiClient(options.api_socket)
    try:
        api_client.connect()
        settings, status = _get_api_config(api_client)
        subscriptions = find_api_subscriptions(
                settings, options.axis, options.chips)
        if not subscriptions:
            opts.error("No accelerometer chips found for API capture")
        sample_buffers = {}
        for sub in subscriptions:
            qid = _subscription_key(sub)
            sample_buffers[qid] = ApiSampleBuffer(sub['method'], sub['sensor'])
            params = dict(sub['params'])
            params['response_template'] = {'q': qid}
            api_client.query(sub['method'], params, sample_buffers)
        result = api_client.query(
                "resonance_tester/run_test", _run_test_params(options),
                sample_buffers)
        datas, api_max_freq = _process_api_windows(
                result.get('capture_windows', []), sample_buffers)
        if options.scv == 5.:
            toolhead = status.get('toolhead', {})
            if 'square_corner_velocity' in toolhead:
                options.scv = toolhead['square_corner_velocity']
        return datas, api_max_freq
    finally:
        api_client.close()

######################################################################
# Startup
######################################################################

def setup_matplotlib(output_to_file):
    global matplotlib
    if output_to_file:
        matplotlib.rcParams.update({'figure.autolayout': True})
        matplotlib.use('Agg')
    import matplotlib.pyplot, matplotlib.dates, matplotlib.font_manager
    import matplotlib.ticker

def main():
    # Parse command-line arguments
    usage = "%prog [options] <logs>"
    opts = optparse.OptionParser(usage)
    opts.add_option("-o", "--output", type="string", dest="output",
                    default=None, help="filename of output graph")
    opts.add_option("-c", "--csv", type="string", dest="csv",
                    default=None, help="filename of output csv file")
    opts.add_option("-f", "--max_freq", type="float", default=None,
                    help="maximum frequency to plot")
    opts.add_option("-s", "--max_smoothing", type="float", dest="max_smoothing",
                    default=None, help="maximum shaper smoothing to allow")
    opts.add_option("-v", "--max_vibrs_pcnt", type="float",
                    dest="max_vibrs_pcnt", default=None, help="maximum " +
                    "remaining shaper vibrations score to allow (in percents)")
    opts.add_option("--scv", "--square_corner_velocity", type="float",
                    dest="scv", default=5., help="square corner velocity")
    opts.add_option("--shaper_freq", type="string", dest="shaper_freq",
                    default=None, help="shaper frequency(-ies) to test, " +
                    "either a comma-separated list of floats, or a range in " +
                    "the format [start]:end[:step]")
    opts.add_option("--shapers", type="string", dest="shapers", default=None,
                    help="a comma-separated list of shapers to test")
    opts.add_option("--damping_ratio", type="float", dest="damping_ratio",
                    default=None, help="shaper damping_ratio parameter")
    opts.add_option("--test_damping_ratios", type="string",
                    dest="test_damping_ratios", default=None,
                    help="a comma-separated list of damping ratios to test " +
                    "input shaper for")
    opts.add_option("--api", type="string", dest="api_socket", default=None,
                    help="Klipper API Unix Domain Socket to capture data from")
    opts.add_option("--axis", type="string", dest="axis", default=None,
                    help="axis to test in --api mode")
    opts.add_option("--chips", type="string", dest="chips", default=None,
                    help="comma-separated accelerometer chip names in --api mode")
    opts.add_option("--point", type="string", dest="point", default=None,
                    help="x,y,z point to test in --api mode")
    opts.add_option("--name", type="string", dest="name", default=None,
                    help="test name passed to Klipper in --api mode")
    opts.add_option("--freq_start", type="float", dest="freq_start",
                    default=None, help="minimum test frequency in --api mode")
    opts.add_option("--freq_end", type="float", dest="freq_end",
                    default=None, help="maximum test frequency in --api mode")
    opts.add_option("--accel_per_hz", type="float", dest="accel_per_hz",
                    default=None, help="test acceleration per Hz in --api mode")
    opts.add_option("--hz_per_sec", type="float", dest="hz_per_sec",
                    default=None, help="frequency sweep rate in --api mode")
    opts.add_option("--sweeping_accel", type="float", dest="sweeping_accel",
                    default=None, help="sweeping acceleration in --api mode")
    opts.add_option("--sweeping_period", type="float", dest="sweeping_period",
                    default=None, help="sweeping period in --api mode")
    opts.add_option("--input_shaping", type="int", dest="input_shaping",
                    default=None, help="pass INPUT_SHAPING to Klipper in --api mode")
    options, args = opts.parse_args()
    if options.api_socket is None and len(args) < 1:
        opts.error("Incorrect number of arguments")
    if options.max_smoothing is not None and options.max_smoothing < 0.05:
        opts.error("Too small max_smoothing specified (must be at least 0.05)")
    if options.max_vibrs_pcnt is not None and options.max_vibrs_pcnt < 0.1:
        opts.error("Too small max_smoothing specified (must be at least 0.1)")

    max_freq = options.max_freq
    if options.shaper_freq is None:
        shaper_freqs = []
    elif options.shaper_freq.find(':') >= 0:
        freq_start = None
        freq_end = None
        freq_step = None
        try:
            freqs_parsed = options.shaper_freq.partition(':')
            if freqs_parsed[0]:
                freq_start = float(freqs_parsed[0])
            freqs_parsed = freqs_parsed[-1].partition(':')
            freq_end = float(freqs_parsed[0])
            if freq_start and freq_start > freq_end:
                opts.error("Invalid --shaper_freq param: start range larger " +
                           "than its end")
            if freqs_parsed[-1].find(':') >= 0:
                opts.error("Invalid --shaper_freq param format")
            if freqs_parsed[-1]:
                freq_step = float(freqs_parsed[-1])
        except ValueError:
            opts.error("--shaper_freq param does not specify correct range " +
                       "in the format [start]:end[:step]")
        shaper_freqs = (freq_start, freq_end, freq_step)
        if max_freq is not None:
            max_freq = max(max_freq, freq_end * 4./3.)
    else:
        try:
            shaper_freqs = [float(s) for s in options.shaper_freq.split(',')]
        except ValueError:
            opts.error("invalid floating point value in --shaper_freq param")
        if max_freq is not None:
            max_freq = max(max_freq, max(shaper_freqs) * 4./3.)
    if options.test_damping_ratios:
        try:
            test_damping_ratios = [float(s) for s in
                                   options.test_damping_ratios.split(',')]
        except ValueError:
            opts.error("invalid floating point value in " +
                       "--test_damping_ratios param")
    else:
        test_damping_ratios = None
    if options.shapers is None:
        shapers = None
    else:
        shapers = re.split(r",(?![^(]*\))", options.shapers.lower())

    # Parse data
    if options.api_socket is not None:
        datas, api_max_freq = capture_api_data(options, opts)
        if max_freq is None:
            max_freq = api_max_freq
    else:
        datas = [parse_log(fn) for fn in args]

    # Calibrate shaper and generate outputs
    selected_shaper, shapers, calibration_data = calibrate_shaper(
            datas, options.csv, shapers=shapers,
            damping_ratio=options.damping_ratio,
            scv=options.scv, shaper_freqs=shaper_freqs,
            max_smoothing=options.max_smoothing,
            max_vibrs_pcnt=options.max_vibrs_pcnt,
            test_damping_ratios=test_damping_ratios,
            max_freq=max_freq)
    if selected_shaper is None:
        return
    if max_freq is None:
        max_freq = 0.
        for data in calibration_data.get_datasets():
            max_freq = max(max_freq, data.freq_bins.max())
    if not options.csv or options.output:
        # Draw graph
        setup_matplotlib(options.output is not None)

        fig = plot_freq_response(calibration_data, shapers,
                                 selected_shaper, max_freq)

        # Show graph
        if options.output is None:
            matplotlib.pyplot.show()
        else:
            fig.set_size_inches(8, 6)
            fig.savefig(options.output)

if __name__ == '__main__':
    main()
