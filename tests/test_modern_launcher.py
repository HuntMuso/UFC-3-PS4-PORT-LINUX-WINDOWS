import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'launcher' / 'modern'))
import ufc3_modern_launcher as ml  # noqa: E402

RTX = [ml.classify_gpu('NVIDIA GeForce RTX 4070', 'NVIDIA')]
GTX = [ml.classify_gpu('NVIDIA GeForce GTX 1060 6GB', 'NVIDIA')]
AMD = [ml.classify_gpu('AMD Radeon RX 7800 XT', 'Advanced Micro Devices, Inc.')]
INTEL = [ml.classify_gpu('Intel(R) UHD Graphics 770', 'Intel Corporation')]


class Fixture(unittest.TestCase):
    """A package root with or without the engine's DLSS module, and a data folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'pkg'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'share').mkdir()
        self.data = Path(self.tmp.name) / 'data'
        self.data.mkdir()
        self.game = Path(self.tmp.name) / 'CUSA06534'
        self.game.mkdir()
        (self.game / 'eboot.bin').write_bytes(b'x')
        self.saved_root, ml.ROOT = ml.ROOT, self.root
        self.saved_env = dict(os.environ)
        for key in ('BB_UPSCALER', 'BB_DLSS_MODE', 'BB_DLSS_FALLBACK', 'BB_FSR1', 'BB_CONFIG', 'BB_LOG_DIR',
                    'UFC3_LAUNCH_COMMAND', 'UFC3_FAKE_GPU', 'BB_ONLINE_SERVER'):
            os.environ.pop(key, None)
        self.addCleanup(self.restore)

    def restore(self):
        ml.ROOT = self.saved_root
        os.environ.clear()
        os.environ.update(self.saved_env)

    def enable_engine_dlss(self):
        (self.root / 'share' / 'engine-features.json').write_text('{"dlss4": true}')
        (self.root / 'bin' / 'nvngx_dlss.dll').write_bytes(b'dll')


class SettingsTests(Fixture):
    def test_defaults_match_the_win32_launcher(self):
        s = ml.load_settings(self.data)
        self.assertEqual((s['resolution'], s['fps_limit'], s['present_mode'], s['sharpness'], s['skip_movies']),
                         (1, '0', 2, 2, True))
        self.assertEqual((s['upscaler'], s['online_mode'], s['language'], s['player']), ('off', 'offline', '1', 'Player 1'))

    def test_round_trip_and_ini_keys_are_the_ones_the_exe_reads(self):
        s = ml.normalize(dict(game=str(self.game), player='Ada', fps_limit='144', resolution=3, upscaler='fsr1',
                              sharpness=3, display=2, language='17', online_mode='join', join_address='100.80.1.2',
                              fullscreen=True, show_fps=False), ml.load_settings(self.data))
        ml.save_settings(self.data, s)
        ini = ml.read_ini(self.data / 'sacredfire.ini')
        self.assertEqual(set(ini), {'player', 'online_mode', 'online_server', 'host_address', 'game', 'gamepad', 'neo',
                                    'fullscreen', 'clear_cache', 'validation', 'frame_stats', 'menu_trace', 'resolution',
                                    'fps_limit', 'present_mode', 'upscaler', 'sharpness', 'display', 'skip_movies',
                                    'language', 'log_dir'})
        self.assertEqual((ini['upscaler'], ini['neo'], ini['online_server'], ini['resolution']), ('1', '1', '100.80.1.2', '3'))
        self.assertEqual(ml.read_ini(self.data / 'bbport.ini')['show_fps'], '0')
        again = ml.load_settings(self.data)
        for key in ('player', 'fps_limit', 'resolution', 'upscaler', 'display', 'language', 'online_mode', 'show_fps'):
            self.assertEqual(again[key], s[key], key)

    def test_old_exe_files_load(self):
        (self.data / 'sacredfire.ini').write_text('fps_mode=2\nonline_server=10.0.0.5\nupscaler=1\n', encoding='utf-8')
        s = ml.load_settings(self.data)
        self.assertEqual((s['fps_limit'], s['online_mode'], s['join_address'], s['upscaler']), ('144', 'join', '10.0.0.5', 'fsr1'))

    def test_keys_added_by_a_newer_exe_survive_a_save(self):
        (self.data / 'sacredfire.ini').write_text('player=Ada\nnew_option=7\nfps_limit=90\n', encoding='utf-8')
        s = ml.normalize(dict(fps_limit='144'), ml.load_settings(self.data))
        ml.save_settings(self.data, s)
        ini = ml.read_ini(self.data / 'sacredfire.ini')
        self.assertEqual((ini['new_option'], ini['fps_limit'], ini['player']), ('7', '144', 'Ada'))
        self.assertFalse((self.data / 'sacredfire.ini').read_text().startswith('#'))

    def test_values_cannot_inject_ini_lines(self):
        s = ml.normalize(dict(player='a\nupscaler=1', game='C:\\x\r\nneo=1', log_dir='x\ny=2'), ml.load_settings(self.data))
        ml.save_settings(self.data, s)
        ini = ml.read_ini(self.data / 'sacredfire.ini')
        self.assertEqual(ini['upscaler'], '0')
        self.assertEqual(ini['neo'], '0')
        self.assertNotIn('y', ini)

    def test_bad_values_fall_back(self):
        s = ml.normalize(dict(resolution=99, fps_limit='7', language='zz', online_mode='x', upscaler='xess', present_mode='no'),
                         ml.load_settings(self.data))
        self.assertEqual((s['resolution'], s['fps_limit'], s['language'], s['online_mode'], s['upscaler']), (1, '0', '1', 'offline', 'off'))

    def test_2k_and_4k_force_ps4_pro(self):
        self.assertTrue(ml.normalize(dict(resolution=2), ml.load_settings(self.data))['neo'])

    def test_player_name_is_printable_ascii_up_to_16(self):
        player = ml.normalize(dict(player='\u00c7a\u011fr\u0131 UFC 3 Champion Belt'), ml.load_settings(self.data))['player']
        self.assertLessEqual(len(player), 16)
        self.assertTrue(all(0x20 <= ord(c) < 0x7f for c in player))
        self.assertTrue(player.startswith('ar UFC 3'))

    def test_existing_bbport_lines_survive_show_fps_edit(self):
        (self.data / 'bbport.ini').write_text('# mine\nkey.cross=X\nshow_fps=1\npatch_x=1\n', encoding='utf-8')
        ml.ini_set_many(self.data / 'bbport.ini', {'show_fps': '0', 'new': '1'})
        text = (self.data / 'bbport.ini').read_text()
        self.assertIn('# mine', text)
        self.assertIn('key.cross=X', text)
        self.assertIn('patch_x=1', text)
        self.assertEqual(ml.read_ini(self.data / 'bbport.ini')['show_fps'], '0')


class ControlsTests(Fixture):
    def test_defaults_and_duplicate_keys(self):
        rows = ml.load_controls(self.data)
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]['key'], 'Space')
        self.assertIsNone(rows[24]['pad'])
        rows[1]['key'] = 'Space'  # taken by Cross
        rows[2]['pad'] = 'not-a-button'
        ml.save_controls(self.data, rows)
        saved = ml.load_controls(self.data)
        self.assertEqual(saved[0]['key'], 'Space')
        self.assertEqual(saved[1]['key'], '')
        self.assertEqual(saved[2]['pad'], 'x')


class GpuTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual([g['vendor'] for g in (RTX + GTX + AMD + INTEL)], ['nvidia', 'nvidia', 'amd', 'intel'])
        self.assertEqual([g['rtx'] for g in (RTX + GTX + AMD + INTEL)], [True, False, False, False])
        self.assertEqual(ml.classify_gpu('Radeon 780M Graphics', '', 'PCI\\VEN_1002&DEV_1900')['vendor'], 'amd')
        self.assertTrue(ml.classify_gpu('RTX A2000', '', 'PCI\\VEN_10DE&DEV_2531')['rtx'])
        self.assertEqual(ml.classify_gpu('Microsoft Basic Render Driver')['vendor'], 'other')

    def test_nvidia_driver_number(self):
        self.assertEqual(ml.nvidia_public_driver('32.0.15.6094'), '560.94')
        self.assertEqual(ml.nvidia_public_driver('31.0.15.3623'), '536.23')
        self.assertEqual(ml.nvidia_public_driver('weird'), 'weird')

    def test_lspci_parse(self):
        text = ('01:00.0 "VGA compatible controller [0300]" "NVIDIA Corporation [10de]" "AD104 [GeForce RTX 4070] [2786]" -ra1\n'
                '00:02.0 "VGA compatible controller [0300]" "Intel Corporation [8086]" "Raptor Lake-S GT1 [UHD Graphics 770] [a780]"\n'
                '00:1f.3 "Audio device [0403]" "Intel Corporation [8086]" "Raptor Lake HD Audio [7a50]"\n')
        gpus = ml.parse_lspci(text)
        self.assertEqual([(g['vendor'], g['rtx']) for g in gpus], [('nvidia', True), ('intel', False)])


class DlssGateTests(Fixture):
    def code(self, gpus, guard=None):
        return ml.dlss_status(gpus, guard or {})['code']

    def test_amd_and_intel_are_locked(self):
        self.enable_engine_dlss()
        self.assertEqual(self.code(AMD), 'no_nvidia')
        self.assertEqual(self.code(INTEL + AMD), 'no_nvidia')
        self.assertEqual(self.code([]), 'no_nvidia')
        status = ml.dlss_status(AMD, {})
        self.assertFalse(status['available'])
        option = [o for o in ml.upscaler_options(status) if o['id'] == 'dlss4'][0]
        self.assertFalse(option['available'])
        self.assertIn('NVIDIA', option['reason'])

    def test_pre_rtx_nvidia_is_locked(self):
        self.enable_engine_dlss()
        self.assertEqual(self.code(GTX), 'not_rtx')

    def test_engine_without_module_stays_locked_even_on_rtx(self):
        self.assertEqual(self.code(RTX), 'engine_missing')
        (self.root / 'share' / 'engine-features.json').write_text('{"dlss4": true}')
        self.assertEqual(self.code(RTX), 'dll_missing')
        (self.root / 'share' / 'engine-features.json').write_text('{"dlss4": false}')
        (self.root / 'bin' / 'nvngx_dlss.dll').write_bytes(b'x')
        self.assertEqual(self.code(RTX), 'engine_missing')

    def test_all_gates_open(self):
        self.enable_engine_dlss()
        self.assertEqual(self.code(RTX), 'ok')
        self.assertTrue(ml.dlss_status(INTEL + RTX, {})['multi_gpu'])
        self.assertFalse(ml.dlss_status(RTX, {})['multi_gpu'])

    def test_crash_guard_locks_until_reset(self):
        self.enable_engine_dlss()
        self.assertEqual(self.code(RTX, {'disabled': True}), 'crash_guard')
        ml.write_json(self.data / 'launcher-modern.json', {'dlss_guard': {'disabled': True}})
        ml.clear_guard(self.data)
        self.assertEqual(ml.load_guard(self.data), {})

    def test_fsr1_and_off_are_always_available(self):
        options = {o['id']: o for o in ml.upscaler_options(ml.dlss_status(AMD, {}))}
        self.assertTrue(options['off']['available'])
        self.assertTrue(options['fsr1']['available'])


class EnvTests(Fixture):
    def settings(self, **extra):
        return ml.normalize(dict(upscaler='dlss4', dlss_mode='balanced', **extra), ml.load_settings(self.data))

    def test_dlss_variables_only_when_available(self):
        self.enable_engine_dlss()
        setv, drop, _ = ml.game_env(self.settings(), ml.dlss_status(RTX, {}), {})
        self.assertEqual(setv, {'BB_UPSCALER': 'dlss4', 'BB_DLSS_MODE': 'balanced', 'BB_DLSS_FALLBACK': 'fsr1', 'BB_FSR1': '0'})
        self.assertEqual(drop, [])

    def test_amd_with_saved_dlss_falls_back_to_fsr1_and_never_sets_dlss(self):
        self.enable_engine_dlss()
        status = ml.dlss_status(AMD, {})
        self.assertEqual(ml.effective_upscaler(self.settings(), status), 'fsr1')
        setv, drop, notes = ml.game_env(self.settings(), status, {})
        self.assertEqual(setv, {'BB_FSR1': '1'})
        self.assertIn('BB_UPSCALER', drop)
        self.assertTrue(any('not available' in n for n in notes))

    def test_engine_without_module_never_receives_dlss(self):
        setv, _, _ = ml.game_env(self.settings(), ml.dlss_status(RTX, {}), {})
        self.assertNotIn('BB_UPSCALER', setv)

    def test_user_environment_wins(self):
        self.enable_engine_dlss()
        setv, _, notes = ml.game_env(self.settings(), ml.dlss_status(RTX, {}), {'BB_UPSCALER': 'off'})
        self.assertNotIn('BB_UPSCALER', setv)
        self.assertTrue(any('BB_UPSCALER' in n for n in notes))

    def test_off_and_fsr1(self):
        status = ml.dlss_status(AMD, {})
        base = ml.normalize({}, ml.load_settings(self.data))
        self.assertEqual(ml.game_env(base, status, {})[0], {})
        self.assertEqual(ml.game_env(dict(base, upscaler='fsr1'), status, {})[0], {'BB_FSR1': '1'})

    def test_dlss_choice_is_kept_beside_the_exe_settings(self):
        self.enable_engine_dlss()
        ml.save_settings(self.data, self.settings())
        self.assertEqual(ml.read_ini(self.data / 'sacredfire.ini')['upscaler'], '0')  # the exe: FSR 1.0 off
        again = ml.load_settings(self.data)
        self.assertEqual((again['upscaler'], again['dlss_mode']), ('dlss4', 'balanced'))
        ini = (self.data / 'sacredfire.ini').read_text().replace('upscaler=0', 'upscaler=1')  # the old window switched FSR on
        (self.data / 'sacredfire.ini').write_text(ini)
        self.assertEqual(ml.load_settings(self.data)['upscaler'], 'fsr1')


FAKE_GAME = 'import sys\nprint("fake game started")\nsys.exit(int(sys.argv[1]))\n'


class CrashGuardTests(Fixture):
    def play(self, exit_code, dlss=True, stop=False):
        self.enable_engine_dlss()
        script = Path(self.tmp.name) / 'fake.py'
        script.write_text(FAKE_GAME)
        os.environ['UFC3_LAUNCH_COMMAND'] = '%s %s %d' % (sys.executable, script, exit_code)
        settings = ml.normalize(dict(game=str(self.game), upscaler='dlss4' if dlss else 'fsr1'), ml.load_settings(self.data))
        session = ml.Session(self.data)
        session.start(settings, ml.dlss_status(RTX, ml.load_guard(self.data)))
        if stop:
            session.user_stop = True
        session.thread.join(20)
        self.assertFalse(session.running)
        return session

    def guard(self):
        return ml.read_json(self.data / 'launcher-modern.json').get('dlss_guard')

    def test_crash_switches_dlss_off(self):
        session = self.play(3)
        self.assertTrue(self.guard()['disabled'])
        self.assertIn('switched off', session.guard_notice)
        self.assertEqual(ml.dlss_status(RTX, self.guard())['code'], 'crash_guard')
        self.assertNotIn('dlss_session_pending', ml.read_json(self.data / 'launcher-modern.json'))

    def test_windows_fault_codes_count_as_crashes(self):
        for code in (23, 142, 0xC0000005):
            self.assertTrue(ml.is_crash(code), code)
        for code in (0, 1, 2, 130):
            self.assertFalse(ml.is_crash(code), code)
        self.assertTrue(ml.is_crash(-11))

    def test_clean_exit_keeps_dlss(self):
        self.play(0)
        self.assertIsNone(self.guard())

    def test_user_stop_and_launcher_failures_are_not_dlss_crashes(self):
        self.play(1, stop=True)
        self.assertIsNone(self.guard())
        self.play(2)
        self.assertIsNone(self.guard())

    def test_crash_without_dlss_does_not_touch_the_guard(self):
        self.play(3, dlss=False)
        self.assertIsNone(self.guard())

    def test_unfinished_dlss_session_is_caught_next_start(self):
        ml.write_json(self.data / 'launcher-modern.json', {'dlss_session_pending': True})
        guard = ml.load_guard(self.data)
        self.assertTrue(guard['disabled'])
        self.assertTrue(guard['unclean'])

    def test_session_log_and_validation(self):
        session = self.play(0)
        text, _ = session.read(0)
        self.assertIn('fake game started', text)
        self.assertIn('Game exited with code 0', text)
        with self.assertRaises(ValueError):
            ml.Session(self.data).start(ml.normalize(dict(game=str(self.game / 'nope')), ml.load_settings(self.data)), ml.dlss_status(AMD, {}))
        with self.assertRaises(ValueError):
            ml.Session(self.data).start(ml.normalize(dict(game=str(self.game), online_mode='join'), ml.load_settings(self.data)), ml.dlss_status(AMD, {}))


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        os.environ['UFC3_FAKE_GPU'] = 'AMD Radeon RX 7800 XT'
        os.environ['BB_DATA_DIR'] = str(self.data)
        self.app = ml.App(self.data)
        self.server = ml.ThreadingHTTPServer(('127.0.0.1', 0), ml.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, host=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        head = {'Host': host or '127.0.0.1:%d' % self.port}
        head.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            head['Content-Type'] = 'application/json'
        conn.request(method, path, data, head)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def url(self, route):
        return '/%s/%s' % (self.app.token, route)

    def test_token_host_and_csrf_guards(self):
        self.assertEqual(self.call('GET', '/wrong/api/state')[0], 404)
        self.assertEqual(self.call('GET', self.url('api/state'), host='evil.example')[0], 403)
        self.assertEqual(self.call('POST', self.url('api/stop'), {})[0], 403)  # no X-UFC3 header
        self.assertEqual(self.call('POST', self.url('api/stop'), {}, headers={'X-UFC3': '1'})[0], 200)

    def test_page_and_state(self):
        status, page = self.call('GET', self.url(''))
        self.assertEqual(status, 200)
        self.assertIn(b'EA SPORTS UFC 3', page)
        status, state = self.call('GET', self.url('api/state'))
        self.assertEqual(status, 200)
        dlss = [u for u in state['upscalers'] if u['id'] == 'dlss4'][0]
        self.assertFalse(dlss['available'])
        self.assertEqual(state['dlss']['code'], 'no_nvidia')
        self.assertEqual(state['settings']['upscaler'], 'off')

    def test_dlss_refused_on_amd_but_other_settings_save(self):
        status, body = self.call('POST', self.url('api/settings'), {'upscaler': 'dlss4'}, headers={'X-UFC3': '1'})
        self.assertEqual(status, 400)
        self.assertIn('NVIDIA', body['error'])
        status, body = self.call('POST', self.url('api/settings'), {'upscaler': 'fsr1', 'fps_limit': '144'}, headers={'X-UFC3': '1'})
        self.assertEqual((status, body['settings']['upscaler'], body['settings']['fps_limit']), (200, 'fsr1', '144'))

    def test_saved_dlss_on_amd_reports_the_fallback_and_other_edits_still_work(self):
        ml.write_json(self.data / 'launcher-modern.json', {'upscaler': 'dlss4'})
        status, state = self.call('GET', self.url('api/state'))
        self.assertEqual((state['settings']['upscaler'], state['effective_upscaler']), ('dlss4', 'fsr1'))
        status, body = self.call('POST', self.url('api/settings'), {'fullscreen': True}, headers={'X-UFC3': '1'})
        self.assertEqual(status, 200)

    def test_play_without_game_folder_is_a_clean_error(self):
        status, body = self.call('POST', self.url('api/play'), {}, headers={'X-UFC3': '1'})
        self.assertEqual(status, 400)
        self.assertIn('game folder', body['error'])

    def test_controls_endpoint(self):
        status, body = self.call('GET', self.url('api/controls'))
        self.assertEqual((status, len(body['rows']), len(body['defaults'])), (200, 25, 25))


if __name__ == '__main__':
    unittest.main()
