"""Anonymous public media failure evidence and unchanged completeness gates."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import production_qwen as pipeline
from src.runtime.audio_preparation import safe_transport_diagnostics, validate_prepared_audio


class MediaFailureAuditTests(unittest.TestCase):
    def test_startup_transport_is_sanitized_without_mutating_retained_specification(self):
        spec={'prepare_phase':'audio_download','audio_startup_diagnostics':{
            'phase':'transport_start','error_code':'media_session_unavailable',
            'source_transport':{'terminal_error_code':'media_session_unavailable',
                'url':'private-url','session_recovery_events':[
                    {'event':'login_redirect','elapsed_seconds':.5,'offset':0,'cookie':'private'}]}}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp}):
            pipeline.preparation_failure_audit(spec,{},RuntimeError('private'))
            public=json.loads(pipeline.out('prepare-failure.json').read_text())
        self.assertNotIn('private',json.dumps(public))
        self.assertEqual(public['audio_startup_diagnostics']['source_transport']['session_recovery_events'],
            [{'event':'login_redirect','elapsed_seconds':.5}])
        self.assertEqual(spec['audio_startup_diagnostics']['source_transport']['url'],'private-url')

    def test_public_failure_identifies_media_resume_failure_without_private_fields(self):
        transport={'terminal_error_code':'media_session_unavailable',
            'range_requests':268,'range_verified':266,'session_refresh_attempts':1,
            'session_identity_verifications':1,'session_resume_attempts':1,'session_refresh_successes':0,
            'upstream_status_counts':{'206':266,'302':2,'private':10},
            'url':'private-url','cookie':'private-cookie','source_total_bytes':3683574402,
            'media_auth':{'user':'private-user','stage':'verified'},
            'session_recovery_events':[
                {'event':'identity_verified','elapsed_seconds':2230.2,'offset':2223046656,'url':'private'},
                {'event':'media_resume_failed','elapsed_seconds':2231.7,'offset':2223046656}]}
        spec={'prepare_phase':'audio_validation','course_id':'private-course','lecture':{'date':'private-date'},
            'audio_seconds':8041.493,'media_seconds':13377.47,
            'audio_diagnostics':{'source_transport':transport,'decode_return_code':0,
                'decode_error_counts':{'premature_eof':2,'input_read_error':2,'private':1}}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RUNNER_TEMP':tmp}):
            pipeline.preparation_failure_audit(spec,{},ValueError('Production audio has read or decode errors'))
            public=json.loads(pipeline.out('prepare-failure.json').read_text())
        self.assertEqual(public['error_code'],'audio_decode_errors')
        self.assertEqual(public['source_transport']['terminal_error_code'],'media_session_unavailable')
        self.assertEqual(public['source_transport']['session_identity_verifications'],1)
        self.assertEqual(public['source_transport']['session_refresh_successes'],0)
        self.assertEqual(public['decode_error_counts'],{'premature_eof':2,'input_read_error':2})
        self.assertNotIn('private',json.dumps(public));self.assertNotIn('2223046656',json.dumps(public))
        self.assertNotIn('3683574402',json.dumps(public))

    def test_malformed_private_fields_cannot_escape_whitelist(self):
        self.assertEqual(safe_transport_diagnostics({'terminal_error_code':[],
            'session_identity_verifications':True,'session_refresh_successes':-1,
            'upstream_status_counts':{'private':1,'200':True},
            'session_recovery_events':[{'event':[],'elapsed_seconds':1},
                {'event':'media_resumed','elapsed_seconds':float('nan'),'private':'private'}]}),
            {'upstream_status_counts':{},'session_recovery_events':[]})

    def test_verified_identity_without_resumed_media_cannot_pass_completeness_gate(self):
        diagnostics={'decode_return_code':0,'stderr_complete':True,'pcm_sample_aligned':True,
            'decode_error_counts':{},'source_transport':{'terminal_error_code':'media_session_unavailable',
                'session_identity_verifications':1,'session_refresh_successes':0}}
        spec={'audio_diagnostics':diagnostics,'audio_seconds':8041.493,'media_seconds':13377.47}
        with self.assertRaisesRegex(ValueError,'read or decode errors'): validate_prepared_audio(spec)
        diagnostics['source_transport']={'terminal_error_code':None,'session_refresh_successes':1}
        with self.assertRaisesRegex(ValueError,'incomplete'): validate_prepared_audio(spec)


if __name__=='__main__': unittest.main()
