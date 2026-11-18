import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('tc_resident', Path(__file__).resolve().parents[1] / 'scripts/tongcheng-resident.py')
resident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resident)


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = resident.PRIVATE
        resident.PRIVATE = Path(self.temp.name)
        resident.save(resident.PRIVATE / 'resident-control.json', {'monitor_enabled': True})
        self.s = resident.Supervisor()
        self.s.child = Mock(pid=123)
        self.s.child.poll.return_value = None
        self.s.client = Mock(csrf='local-test')
        self.live = {'mode': 'shadow', 'active': True, 'transport': {'online': True}}
        self.s.client.call.return_value = self.live

    def tearDown(self):
        resident.PRIVATE = self.original
        self.temp.cleanup()

    def test_pause_and_other_modes_never_resume(self):
        for mode in ('draft', 'readonly', 'test_auto'):
            self.assertFalse(resident.should_resume({'monitor_enabled': True}, {'mode': mode, 'active': False}))
        self.assertFalse(resident.should_resume({'monitor_enabled': False}, {'mode': 'shadow', 'active': False}))
        resident.save(resident.PRIVATE / 'resident-control.json', {'monitor_enabled': False})
        self.live['active'] = False
        self.s.tick()
        self.assertEqual([c.args[0] for c in self.s.client.call.call_args_list], ['/shadow/live/data'])

    def test_healthy_does_not_interrupt_service(self):
        self.s.tick()
        self.s.child.terminate.assert_not_called()
        self.assertEqual([c.args[0] for c in self.s.client.call.call_args_list], ['/shadow/live/data'])

    def test_explicit_production_intent_resumes_but_pause_wins(self):
        resident.save(resident.PRIVATE/'production-intent.json',{'enabled':True})
        self.live['mode']='draft';self.live['active']=False
        self.s.tick()
        self.assertIn('/production/resume',[c.args[0] for c in self.s.client.call.call_args_list])
        self.s.client.reset_mock();self.s.next_resume=0
        resident.save(resident.PRIVATE/'resident-control.json',{'monitor_enabled':False})
        self.s.tick()
        self.assertNotIn('/production/resume',[c.args[0] for c in self.s.client.call.call_args_list])

    def test_crashed_child_records_exit_and_schedules_restart(self):
        self.s.child.poll.return_value = 9
        self.s.tick()
        self.assertIsNone(self.s.child)
        self.assertEqual(self.s.last_exit['code'], 9)
        self.assertEqual(self.s.restarts, 1)
        self.assertGreater(self.s.next_start, 0)
        events=(resident.PRIVATE / 'resident-events.jsonl').read_text('utf-8')
        self.assertIn('service_exited', events)

    def test_health_requires_three_failures_and_does_not_log_exception_secrets(self):
        self.s.client.call.side_effect = RuntimeError('private-test-secret')
        self.s.tick();self.s.tick()
        self.s.child.terminate.assert_not_called()
        self.s.tick()
        self.s.child.terminate.assert_called_once()
        self.assertNotIn('private-test-secret', (resident.PRIVATE / 'resident-events.jsonl').read_text('utf-8'))

    def test_slow_startup_has_grace_instead_of_restart_loop(self):
        import time
        self.s.startup_until=time.time()+60
        self.s.client.call.side_effect=TimeoutError()
        for _ in range(4): self.s.tick()
        self.s.child.terminate.assert_not_called()
        status=json.loads((resident.PRIVATE/'resident-status.json').read_text('utf-8'))
        self.assertEqual(status['health_state'],'starting')
        self.assertEqual(status['consecutive_health_failures'],4)

    def test_shadow_resume_uses_only_local_open_start_and_rechecks_pause(self):
        self.live['active'] = False
        self.s.tick()
        paths=[c.args[0] for c in self.s.client.call.call_args_list]
        self.assertEqual(paths, ['/shadow/live/data', '/open', '/shadow/live/data', '/start', '/shadow/live/data'])
        self.s.client.reset_mock()
        def call(path, *args, **kwargs):
            if path == '/open':
                resident.save(resident.PRIVATE / 'resident-control.json', {'monitor_enabled': False})
            return self.live
        self.s.client.call.side_effect = call
        self.s.next_resume=0
        self.s.tick()
        self.assertNotIn('/start', [c.args[0] for c in self.s.client.call.call_args_list])


if __name__ == '__main__':
    unittest.main()
