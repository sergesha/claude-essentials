"""Ownership boundary for the EffectCoordinator responsibility bases."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
import textwrap
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

_VALUE_MODULE = "lockstep.runtime.effects._coordinator_values"
_FACADE_MODULE = "lockstep.runtime.effects.coordinator"
_VALUE_DEFINITIONS = {
    "CoordinatorLineageError",
    "ProviderContractViolation",
    "ReconcileReport",
    "_Context",
    "_PublicationItemContext",
}
_BASES = {'lockstep.runtime.effects._coordinator_validation': ('_EffectCoordinatorValidation',
                                                      ('_raw_descriptor',
                                                       '_protected',
                                                       '_deadline_candidates',
                                                       '_check_reconciliation_boundary',
                                                       '_check_launch',
                                                       '_closed_result',
                                                       '_check_observation',
                                                       '_validate_runtime_input_boundary',
                                                       '_timeout_result',
                                                       '_terminal_safety',
                                                       '_interrupt_values',
                                                       '_publication_result',
                                                       '_publication_error_result')),
 'lockstep.runtime.effects._coordinator_context': ('_EffectCoordinatorContext',
                                                   ('_context',)),
 'lockstep.runtime.effects._coordinator_lineage': ('_EffectCoordinatorLineage',
                                                   ('_binding',
                                                    '_ancestor_results')),
 'lockstep.runtime.effects._coordinator_context_input': ('_EffectCoordinatorContextInput',
                                                         ('_scope_context',
                                                          '_effect_intent',
                                                          '_resolved_effect_context')),
 'lockstep.runtime.effects._coordinator_runner_resolution': ('_EffectCoordinatorRunnerResolution',
                                                             ('_runner_for',
                                                              '_runner_for_binding',
                                                              '_effect_runner_and_scopes')),
 'lockstep.runtime.effects._coordinator_publication': ('_EffectCoordinatorPublication',
                                                       ('_reconcile_publication',)),
 'lockstep.runtime.effects._coordinator_publication_planning': ('_EffectCoordinatorPublicationPlanning',
                                                               ('_publisher_for',
                                                                '_publication_ancestor_results',
                                                                '_publication_artifact',
                                                                '_publication_item_context',
                                                                '_publication_effect_intent',
                                                                '_bound_publication_request',
                                                                '_publication_intent',
                                                                '_reconcile_acceptance',
                                                                '_publication_lease')),
 'lockstep.runtime.effects._coordinator_publication_recovery': ('_EffectCoordinatorPublicationRecovery',
                                                               ('_commit_publication_recovery',
                                                                '_recover_publication',
                                                                '_commit_prepared_publication')),
 'lockstep.runtime.effects._coordinator_publication_recovery_policy': ('_EffectCoordinatorPublicationRecoveryPolicy',
                                                                      ('_publication_recovery_guard_is_current',)),
 'lockstep.runtime.effects._coordinator_publication_recovery_transaction': ('_EffectCoordinatorPublicationRecoveryTransaction',
                                                                           ('_capture_publication_successor',
                                                                            '_publication_recovery_receipt',
                                                                            '_guarded_publication_recovery',
                                                                            '_finalize_publication_recovery')),
 'lockstep.runtime.effects._coordinator_publication_existing': ('_EffectCoordinatorPublicationExisting',
                                                               ('_publication_existing_result',)),
 'lockstep.runtime.effects._coordinator_publication_preparation': ('_EffectCoordinatorPublicationPreparation',
                                                                  ('_prepare_publication_effect',)),
 'lockstep.runtime.effects._coordinator_publication_commitment': ('_EffectCoordinatorPublicationCommitment',
                                                                 ('_validate_publication_authority',
                                                                  '_prepared_publication_commitment')),
 'lockstep.runtime.effects._coordinator_publication_transition': ('_EffectCoordinatorPublicationTransition',
                                                                 ('_advance_publication_effect',)),
 'lockstep.runtime.effects._coordinator_foundation': ('_EffectCoordinatorFoundation',
                                                      ('_now',
                                                       '_identity',
                                                       '_reconcile_decision',
                                                       '_protected_lineage',
                                                       '_acquire',
                                                       '_report',
                                                       '_manual_handoff',
                                                       '_admit_artifacts')),
 'lockstep.runtime.effects._coordinator_delivery': ('_EffectCoordinatorDelivery',
                                                    ('_requested_delivery_ids',
                                                     '_deliverable_records',
                                                     '_lock_deliverable_records',
                                                     '_commit_deliverable_records',
                                                     'deliver_ready')),
 'lockstep.runtime.effects._coordinator_runner': ('_EffectCoordinatorRunner',
                                                  ('_reconcile_prepared_effect',
                                                   '_commit_runner_launch',
                                                   '_launch_observation_report',
                                                   '_reconcile_launching_effect',
                                                   '_reconcile_expired_running_effect',
                                                   '_adopt_effect_successor',
                                                   '_reconcile_live_running_effect',
                                                   '_reconcile_running_effect',
                                                   '_dispatch_effect_phase',
                                                   '_reconcile_context',
                                                   '_prepare_new_effect',
                                                   '_definitive_prelaunch_result')),
 'lockstep.runtime.effects._coordinator_authority_recovery': ('_EffectCoordinatorAuthorityRecovery',
                                                             ('_authority_blocked_observation',
                                                              '_commit_authority_blocked_observation')),
 'lockstep.runtime.effects._coordinator_orchestration': ('_EffectCoordinatorOrchestration',
                                                         ('_reconcile_inventory',
                                                          '_recover_missing_effect',
                                                          '_delivered_coordinate_retry',
                                                          '_select_reconcile_effect',
                                                          '_current_record_under_lease',
                                                          '_reconcile_special_descriptor',
                                                          '_reconcile_owned_effect')),
 'lockstep.runtime.effects._coordinator_reconciliation': ('_EffectCoordinatorReconciliation',
                                                          ('reconcile',
                                                          'reconcile_one',
                                                          'reconcile_pending',
                                                          'reconcile_consumed',
                                                          'reconcile_due',
                                                          'next_wakeup_delay',
                                                          'wait_and_reconcile_due')),
 'lockstep.runtime.effects._coordinator_admission': ('_EffectCoordinatorAdmission',
                                                     ('_manual_submission_context',
                                                      '_commit_manual_submission',
                                                      'submit_manual',
                                                      '_acceptance_commitment',
                                                      '_pending_acceptance',
                                                      'preview_acceptance',
                                                      'issue_acceptance_consent',
                                                      '_acceptance_retry_commitment',
                                                      '_acceptance_retry_state',
                                                      '_acceptance_retry_expected_result',
                                                      '_validate_acceptance_retry_record',
                                                      '_validate_acceptance_retry_producer',
                                                      '_validate_acceptance_retry_lineage',
                                                      '_redeem_delivered_acceptance_retry',
                                                      '_acceptance_submission_context',
                                                      '_commit_acceptance_submission',
                                                      'submit_acceptance'))}
_ALLOWED_OWNER_DEPENDENCIES = {('admission', 'delivery'),
 ('admission', 'foundation'),
 ('admission', 'lineage'),
 ('admission', 'validation'),
 ('context', 'context_input'),
 ('context', 'foundation'),
 ('context', 'lineage'),
 ('context', 'runner_resolution'),
 ('context', 'validation'),
 ('context_input', 'runner_resolution'),
 ('delivery', 'foundation'),
 ('delivery', 'lineage'),
 ('delivery', 'validation'),
 ('foundation', 'validation'),
 ('lineage', 'validation'),
 ('orchestration', 'foundation'),
 ('orchestration', 'lineage'),
 ('orchestration', 'publication'),
 ('orchestration', 'publication_planning'),
 ('orchestration', 'runner'),
 ('orchestration', 'validation'),
 ('publication', 'publication_planning'),
 ('publication', 'publication_commitment'),
 ('publication', 'publication_preparation'),
 ('publication', 'publication_existing'),
 ('publication', 'publication_transition'),
 ('publication_planning', 'foundation'),
 ('publication_planning', 'validation'),
 ('publication_recovery', 'foundation'),
 ('publication_recovery', 'publication_planning'),
 ('publication_recovery', 'publication_recovery_transaction'),
 ('publication_recovery', 'validation'),
 ('publication_recovery_transaction', 'foundation'),
 ('publication_recovery_transaction', 'publication_recovery_policy'),
 ('publication_recovery_transaction', 'validation'),
 ('publication_preparation', 'foundation'),
 ('publication_existing', 'foundation'),
 ('publication_existing', 'publication_recovery'),
 ('publication_transition', 'foundation'),
 ('publication_transition', 'publication_recovery'),
 ('reconciliation', 'foundation'),
 ('reconciliation', 'lineage'),
 ('reconciliation', 'orchestration'),
 ('reconciliation', 'validation'),
 ('runner', 'authority_recovery'),
 ('runner', 'context'),
 ('runner', 'foundation'),
 ('runner', 'validation'),
 ('runner_resolution', 'validation'),
 ('authority_recovery', 'foundation'),
 ('authority_recovery', 'runner_resolution'),
 ('authority_recovery', 'validation')}
_PUBLIC_API = {
    "reconcile", "reconcile_one", "reconcile_pending", "reconcile_consumed",
    "submit_manual", "preview_acceptance", "issue_acceptance_consent",
    "submit_acceptance", "deliver_ready", "reconcile_due",
    "next_wakeup_delay", "wait_and_reconcile_due",
}
_INSTANCE_FIELDS = {
    "_artifacts", "_authority", "_catalog", "_clock", "_lease_ttl",
    "_leases", "_ledger", "_manual", "_owner_factory", "_publisher",
    "_publisher_resolver", "_runner_bindings", "_runners", "_runtime",
    "_snapshot_resolver",
}
_IMPORT_ORDER = (
    "lockstep.runtime.effects._coordinator_validation",
    "lockstep.runtime.effects._coordinator_foundation",
    "lockstep.runtime.effects._coordinator_lineage",
    "lockstep.runtime.effects._coordinator_runner_resolution",
    "lockstep.runtime.effects._coordinator_context_input",
    "lockstep.runtime.effects._coordinator_delivery",
    "lockstep.runtime.effects._coordinator_publication_planning",
    "lockstep.runtime.effects._coordinator_publication_recovery_policy",
    "lockstep.runtime.effects._coordinator_authority_recovery",
    "lockstep.runtime.effects._coordinator_context",
    "lockstep.runtime.effects._coordinator_publication_recovery_transaction",
    "lockstep.runtime.effects._coordinator_publication_recovery",
    "lockstep.runtime.effects._coordinator_publication_existing",
    "lockstep.runtime.effects._coordinator_publication_preparation",
    "lockstep.runtime.effects._coordinator_publication_commitment",
    "lockstep.runtime.effects._coordinator_publication_transition",
    "lockstep.runtime.effects._coordinator_admission",
    "lockstep.runtime.effects._coordinator_publication",
    "lockstep.runtime.effects._coordinator_runner",
    "lockstep.runtime.effects._coordinator_orchestration",
    "lockstep.runtime.effects._coordinator_reconciliation",
)
_ALLOWED_ONE_HOP_CANDIDATES = {
    "_reconcile_launching_effect",
    "deliver_ready",
    "submit_acceptance",
}
_DECOMPOSED_METHODS = {
    "_commit_publication_recovery",
    "_reconcile_publication",
    "_reconcile_context",
}
_NEW_HELPERS = {
    "_authority_blocked_observation",
    "_commit_authority_blocked_observation",
    "_publication_recovery_guard_is_current",
    "_publication_recovery_receipt",
    "_guarded_publication_recovery",
    "_finalize_publication_recovery",
    "_publication_existing_result",
    "_prepare_publication_effect",
    "_validate_publication_authority",
    "_prepared_publication_commitment",
    "_advance_publication_effect",
    "_acceptance_retry_commitment",
    "_acceptance_retry_state",
    "_acceptance_retry_expected_result",
    "_validate_acceptance_retry_record",
    "_validate_acceptance_retry_producer",
    "_validate_acceptance_retry_lineage",
}
_TYPE_HINT_IDENTITIES = {
    "_advance_publication_effect": {"return": "ReconcileReport"},
    "_admit_artifacts": {"context": "_Context"},
    "_adopt_effect_successor": {"context": "_Context"},
    "_commit_authority_blocked_observation": {"return": "ReconcileReport"},
    "_commit_prepared_publication": {"return": "ReconcileReport"},
    "_commit_publication_recovery": {"return": "ReconcileReport"},
    "_commit_runner_launch": {"context": "_Context"},
    "_context": {"return": "_Context"},
    "_dispatch_effect_phase": {
        "context": "_Context",
        "return": "ReconcileReport",
    },
    "_finalize_publication_recovery": {"return": "ReconcileReport"},
    "_launch_observation_report": {"return": "ReconcileReport"},
    "_prepare_new_effect": {
        "context": "_Context",
        "return": "ReconcileReport",
    },
    "_prepare_publication_effect": {"return": "ReconcileReport"},
    "_publication_item_context": {"return": "_PublicationItemContext"},
    "_reconcile_acceptance": {"return": "ReconcileReport"},
    "_reconcile_decision": {"return": "ReconcileReport"},
    "_reconcile_expired_running_effect": {"context": "_Context"},
    "_reconcile_launching_effect": {
        "context": "_Context",
        "return": "ReconcileReport",
    },
    "_reconcile_live_running_effect": {"context": "_Context"},
    "_reconcile_owned_effect": {"return": "ReconcileReport"},
    "_reconcile_prepared_effect": {
        "context": "_Context",
        "return": "ReconcileReport",
    },
    "_reconcile_publication": {"return": "ReconcileReport"},
    "_reconcile_running_effect": {
        "context": "_Context",
        "return": "ReconcileReport",
    },
    "_report": {"return": "ReconcileReport"},
    "_resolved_effect_context": {"return": "_Context"},
    "_scope_context": {"return": "_Context"},
    "_terminal_safety": {"context": "_Context"},
    "reconcile": {"return": "ReconcileReport"},
    "reconcile_one": {"return": "ReconcileReport"},
}
_EXPECTED_METHOD_DIGESTS = {'__init__': '48266efa798f0c11908e5011c0c264eebcc392656f57cb1b789da535cc977a23',
 '_acceptance_commitment': '89aed1f2cbcfdddef97869f7aaa2d464decb09cb06d936245ea718adbda53cea',
 '_acceptance_submission_context': '4349eac487fb4441a8f82baf35430646b72e1a6db2c5ea18cfc8c77af9bc0a4a',
 '_acquire': '93e4f131db9bfee6f81bc49d8fabe144f4457634705cd0a14e6b0212cafafde2',
 '_admit_artifacts': '3028af1a158046e1c047ceb66a43b72f95d9138a44790f89941b120db367b4f2',
 '_adopt_effect_successor': '60f7aefb3525121e17f1e90de816fa11513e25da52d3a638bd3b376834b97d74',
 '_ancestor_results': 'ba3d4a92b6b7129f273be06caae61357bf90ed4a2729db85b6a1656e3c8c940c',
 '_binding': '8938490c8731cd51c553e4ceac8e6151029719b36764465cc19f85b6d7d9c4cd',
 '_bound_publication_request': '093a3f7f8a8d34dcd03e98f98701a1886e34576a64ca4d0f041eaa7bb19b22ec',
 '_capture_publication_successor': '3a79c160c1f114e93efe11c2e50b081046d7667c35267b722793ba50b255a413',
 '_check_launch': '047484549be4c8429df28a8160fb7e9afd294bd78ca68b772f9b20abdeda2ba7',
 '_check_observation': 'c9a75b53aa9226fda05df7d30def1d5d733c9b16eb91d80ebc497af11ac91626',
 '_check_reconciliation_boundary': 'dc0d607e071b0b4f3cefe1f2aac24f11ff8b779c07ce76a659599a612b550e84',
 '_closed_result': '7e6013436ec15c1377d2e5f956805215c0930ea3dae7009bed9afea4ad043027',
 '_commit_acceptance_submission': '34c1c6068b185597d1d36a3d425105af4ddf852de07ac7d6a01f55e9a6fc769b',
 '_commit_deliverable_records': 'f30101ede3a9fcf3473fe18c9a64ae2d1403b1798ec695ed4bc57145db54c38a',
 '_commit_manual_submission': '7fc1b6d20d16fb07a62783eda174c604ba272531073f5caab04d8f1c47b7dec5',
 '_commit_prepared_publication': 'd2df7b863ad1e3d0e170bc4b4861520100034ccce29e3f9c7f99b3b188f9e0f9',
 '_commit_publication_recovery': '07fa149d89dca4b5ec221589f4eacc106d7ef5318f8898e60cf2dde1eb14c8cb',
 '_commit_runner_launch': 'bd9f388eb876f7848b8eed2829593844a046b7c49dfd137254a6b01285e6055a',
 '_context': 'c77f27dadfbc269fb15fa3b0eff0f30f50ce6467a82305aad43c277edb1ac3a5',
 '_current_record_under_lease': 'b2bd93149e789cbc4c30dabb62a8448631af9d35acb9e4c2dcedaddc71f2060e',
 '_deadline_candidates': '903986cdff8ffb29005b58486a5aa06b870d79a8fa0abf8e686df46d6fa265f6',
 '_definitive_prelaunch_result': '78518032fdbe7be53f2ba8d34f3290ba5399a701da4b24c5c3b7342a8bfcd382',
 '_deliverable_records': '5d2243722b97bf46cbaa499e8c4ed6865d67608b05b4d2397a919dedd1b97f4e',
 '_delivered_coordinate_retry': '7e8507eb0504024b7c9afe963b6fc42def16be2746b355ade9b95615b53f8dca',
 '_dispatch_effect_phase': 'f9653868990f5529b6835975b03411e1a2ffd5614e4aa1beb517a38aaa9ff01e',
 '_effect_intent': '88b4b5df2b7a5672860be41542e8c3a7f38b8cb331fc2b306d045f8b2a1b62c7',
 '_effect_runner_and_scopes': '278c22371dc7e58d696f6acd5e00ed70a8718d58028e512df9c55d47deac74cc',
 '_identity': '3cc8278ae4652bd4f11f851ed9866d5de493b076c7b545ea7415bb591c8b99b9',
 '_interrupt_values': 'cdcd55b4e7a06870cc23b4ea1854c298766ed7006f1bc2ac6d51b1efe75da912',
 '_launch_observation_report': 'bae931b6d29ab7a75cd8179f4bed9b88929f2dedb122e82557e7a0139bcfb9cd',
 '_lock_deliverable_records': '86ba2929d1def9e140d2f97fae218768ff7e5917450fd8d47b564b590b1837c8',
 '_manual_handoff': '82090e720e128d5e699a61bbc43a31eef6562d025b26667cc1b472be08dcc05c',
 '_manual_submission_context': 'e43c6e5fdc2fcce1c4fb2478061f80b207e1229687e67660cafc15073c3c172f',
 '_now': 'e3825c84e3763fc8decff3c179f32f743b30f8325a7f02940e4d015ec3d48aaa',
 '_pending_acceptance': '2582411ee27fce0cf2c23bbe1312f40e2ef21e9718ca5c8de53f638ad2696bb5',
 '_prepare_new_effect': '8b022c43aa1c65c3d26b4e88d2555866398963b645301681db24b0177c3ff6c1',
 '_protected': 'dc866795d7d6ffec5fae63782b671f75ca98d1a4185027cd8b2ec6867b9578c5',
 '_protected_lineage': 'f266f105fe0cc1ec2654ad242b3056ee46406e5a14bfb8027e3b216200cb7fcc',
 '_publication_ancestor_results': '336b7b1a7ea13a2e7882a408ac7569b99b99358190070867a35ed91a57cea33c',
 '_publication_artifact': '1e73feba70fca27fa4b9c4a516640ea09a44793db191c085686fd262f64ba189',
 '_publication_effect_intent': '6345e1fe3c3872eaf3be1ddab60e52f7696a8418e743a392253d1377589c7cc3',
 '_publication_error_result': 'eac8b849100a849f0780c03dd5415cb08a896405c145a706e4007e20bcf2bce8',
 '_publication_intent': 'ea362a3be29aa199b2eb83291af3f5ef98fb46375a9f6d8b02ee71921dde6632',
 '_publication_item_context': '12e60088ad8516ee99114383f7c9168d2b4c0928d2ab46730f9eded11ac8e692',
 '_publication_lease': 'a6d46162b1b0abb7e03a5e65c4c839b5d5c7980f05795be01d8503b3035dc1d3',
 '_publication_result': 'd02e9a5104fa800e73d4e761927ca2393d05156237d08f6dff121c99767839c7',
 '_publisher_for': 'e6de346cf0923a4299486d184101a7af9fbb658b7cc1bbd65dd2a20544c6d6ca',
 '_raw_descriptor': 'b007113e98a101ff170b7bd20ae418dd675cc81ad4ccd291ee2d8f8c4a2c1f8e',
 '_reconcile_acceptance': '8c8f265d7b7560f0293d2c43bdc9e40d85a65d355fa6f06589aeaf4fb3df4bcd',
 '_reconcile_context': '3b85430a3119394e7b838d487f453063a0d6f817aecaa19203235b6af4286f69',
 '_reconcile_decision': 'e915cdf664fc83be4b1a2960e266151ac6f2cefa40ca3d654d96f368123f056e',
 '_reconcile_expired_running_effect': '06582eeffd384474e9c0af4aecd6cbdada566574e9b0bf222837a7f2f9189eac',
 '_reconcile_inventory': 'fd49b54f73ea03e41af76f52b02fa84153dea29ce9193223b0b988c12fdc712c',
 '_reconcile_launching_effect': 'f7759159899b03cbe8aeacaa5b91c1049b5329f5a566ca686ddec6696c864592',
 '_reconcile_live_running_effect': 'c06f8a8523c2fb86cd52d8261948c977daab981efcebcb4af24c38dd551ca908',
 '_reconcile_owned_effect': 'c7f5e12f14c8dafe55eeb14d73a486b07f52776bd422f91a5f8463237a8de972',
 '_reconcile_prepared_effect': '385f004f51580eea329de614e4d981aa1721a249299d73370ad8011de92c912b',
 '_reconcile_publication': 'c55f32f3e3ed92cc7cf0f046db0ed296178507625ddd3a70eb5215a980b06541',
 '_reconcile_running_effect': '014064a7ff620911c61f0653a21a76fa8eb9dc26e844249c16c20eb7640c73c4',
 '_reconcile_special_descriptor': 'a7f45f16b9598880726fdfce8a4af429e399fd20d14314c91c2c51f0a49cc0e1',
 '_recover_missing_effect': 'c99f7e2adc96dc4a8d2515b008fd264c3a18bd85b841cf019ddac144997ff438',
 '_recover_publication': '9ecd3ffcfc80ad615ef8f49b731c926e1aca3b9833086a3895a0a4997c8547f7',
 '_redeem_delivered_acceptance_retry': 'ff407722f1a086365525090de83f9eb3c881ea48cf70841e29a2463d1a58f1fd',
 '_report': 'eacbfe8343f8b5f71982b12a933ca485928f66c214b3bc5ab21feaab03a0ca52',
 '_requested_delivery_ids': '94b153389aaaf0b785225b4a8830fa52e76715a7a6f8528e189f482579ed5134',
 '_resolved_effect_context': 'ae64944f07e889a877f28b1ea692e4f5dec0aa077f055d14890680e128e117da',
 '_runner_for': '700a8b673f85f689c4d9ff1e6cf92dfc1b73ef2eebc65fc960e044d1fb7fa128',
 '_runner_for_binding': 'b48714278a6fb0824bda693bb9de045c6f1a328d56bd228d7315a0c210f32df4',
 '_scope_context': '652f0d6fa2b38442b4ac3082207cf024b794ab0238d12bdf1ed18a0bb3bee480',
 '_select_reconcile_effect': '20f6e75191433fb1da3ec70ee69bc26a3eec4e1ba651c1d5527241c9a0c0d20b',
 '_terminal_safety': '5be10a3c5fa26a2eaae353b4b7215ec756894fb11846c6dacbeba2b8a424f7f0',
 '_timeout_result': '063c041d973eeed44fd225deba3c64fee30673e012fbd554d23dafdef7c65717',
 '_validate_runtime_input_boundary': 'c4a5a268d9694b79fc89719e4d357ed28ad7f3a27c01e583f49a809798c45e23',
 'deliver_ready': 'd9da11745b21a2f7b89accf29e3e781707974c38c5a4875efd9a8b16913c511e',
 'issue_acceptance_consent': 'a60b8ad6250d45a887d4e45bb18e8e2d6430e014149e2e6852b45abd6f3d98a2',
 'next_wakeup_delay': '19d44df3bd2b9e4d0e44f1e2f3ac569ceeae2e9f64007774897fdb2c147fc578',
 'preview_acceptance': '8bc0b3bc3a25dae82dbda8d3e8fa306e410e14739a8598ad0dd04b9d09018da4',
 'reconcile': '36ea7f497bf90cd62f630a7df39895e6cfb52badf86aea3037abc9a0c0b6eaf4',
 'reconcile_consumed': '14d23f967d46e63dbe72e8c1275ca0cf415e2350a6b94360b6a05efbc373e81d',
 'reconcile_due': 'bd36381e368f9ddbd4a22eed9c4fd7e636f7a443c40b09009b7219bce332a641',
 'reconcile_one': '118f216c6461c73bf249c1df89e17d9994b1008f490b70bc2e29fba8a88e168e',
 'reconcile_pending': '3bbddd380ceae0cd2452807a5a8ef24bd33029b593925e0bb8d270ba9ecc1c7e',
 'submit_acceptance': 'e65a5b44d7687cf278c640ff0cea1c23c96799dcb2d7dd8b0a94d88b1717871f',
 'submit_manual': '2e841dba8cff9c14eb89c80f949fa9c1ab31659e99e71f7de2a0afff793dcb47',
 'wait_and_reconcile_due': '3f98f9edef0f7631da93121ec352a5277667d9728946790991e9e5468726c691'}


def _tree(value: object) -> ast.Module:
    return ast.parse(textwrap.dedent(inspect.getsource(value)))


def _top_definitions(module: object) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.parse(inspect.getsource(module)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _method_nodes(value: type) -> dict[str, ast.AST]:
    class_node = next(node for node in _tree(value).body if isinstance(node, ast.ClassDef))
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _digest(node: ast.AST) -> str:
    def stable_dump(value: object) -> str:
        if isinstance(value, ast.AST):
            fields: list[str] = []
            for name, member in ast.iter_fields(value):
                if not isinstance(value, ast.Constant) and (
                    member is None or member == []
                ):
                    continue
                if isinstance(value, ast.Constant) and name == "kind" and member is None:
                    continue
                fields.append(f"{name}={stable_dump(member)}")
            return f"{type(value).__name__}({', '.join(fields)})"
        if isinstance(value, list):
            return f"[{', '.join(stable_dump(member) for member in value)}]"
        return repr(value)

    return hashlib.sha256(stable_dump(node).encode()).hexdigest()


def _ordered_calls(node: ast.AST) -> tuple[str, ...]:
    calls: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, member: ast.Call) -> None:
            calls.append(ast.unparse(member.func))
            self.generic_visit(member)

    Visitor().visit(node)
    return tuple(calls)


def test_coordinator_has_exact_values_facade_and_twenty_one_responsibility_bases() -> None:
    payload = json.dumps(
        {
            "value_module": _VALUE_MODULE,
            "value_definitions": sorted(_VALUE_DEFINITIONS),
            "facade_module": _FACADE_MODULE,
            "bases": [
                [module_name, class_name, list(methods)]
                for module_name, (class_name, methods) in _BASES.items()
            ],
            "import_order": list(_IMPORT_ORDER),
            "public_api": sorted(_PUBLIC_API),
            "instance_fields": sorted(_INSTANCE_FIELDS),
            "type_hint_identities": _TYPE_HINT_IDENTITIES,
        },
        sort_keys=True,
    )
    script = r'''
import ast
import importlib
import inspect
import json
import sys
import typing

spec = json.loads(sys.argv[1])
value_name = spec["value_module"]
facade_name = spec["facade_module"]
base_specs = spec["bases"]
base_names = {item[0] for item in base_specs}


def module_tree(module):
    return ast.parse(inspect.getsource(module))


def top_definitions(module):
    return {
        node.name: node
        for node in module_tree(module).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def method_nodes(value):
    class_node = next(
        node for node in ast.parse(inspect.getsource(value)).body
        if isinstance(node, ast.ClassDef)
    )
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


values = importlib.import_module(value_name)
assert not (base_names & sys.modules.keys())
assert facade_name not in sys.modules
assert set(top_definitions(values)) == set(spec["value_definitions"])
for name in ("ReconcileReport", "_Context", "_PublicationItemContext"):
    node = top_definitions(values)[name]
    assert isinstance(node, ast.ClassDef)
    assert [ast.unparse(item) for item in node.decorator_list] == [
        "dataclass(frozen=True)"
    ]

modules = {}
for module_name in spec["import_order"]:
    assert module_name not in sys.modules
    modules[module_name] = importlib.import_module(module_name)
    assert facade_name not in sys.modules

validation_name = spec["import_order"][0]
validation = modules[validation_name]
assert "EffectCoordinator" not in validation.__dict__

base_types = []
for module_name, class_name, expected_methods in base_specs:
    module = modules[module_name]
    base_type = getattr(module, class_name)
    assert set(top_definitions(module)) == {class_name}
    assert set(method_nodes(base_type)) == set(expected_methods)
    assert base_type.__module__ == module_name
    assert "__slots__" not in base_type.__dict__
    base_types.append(base_type)

facade = importlib.import_module(facade_name)
assert set(top_definitions(facade)) == {"EffectCoordinator"}
assert set(method_nodes(facade.EffectCoordinator)) == {"__init__"}
assert facade.EffectCoordinator.__bases__ == tuple(base_types)
assert "__slots__" not in facade.EffectCoordinator.__dict__
assert facade.EffectCoordinator.MAX_DUE_PER_SCAN == 128
assert all(callable(getattr(facade.EffectCoordinator, name)) for name in spec["public_api"])
assert all(
    getattr(facade, name) is getattr(values, name)
    for name in spec["value_definitions"]
)

initializer = method_nodes(facade.EffectCoordinator)["__init__"]
stored = {
    node.attr
    for node in ast.walk(initializer)
    if isinstance(node, ast.Attribute)
    and isinstance(node.ctx, ast.Store)
    and isinstance(node.value, ast.Name)
    and node.value.id == "self"
}
assert stored == set(spec["instance_fields"])

assert validation.EffectCoordinator is facade.EffectCoordinator
assert (
    validation._EffectCoordinatorValidation._check_observation.__globals__[
        "EffectCoordinator"
    ]
    is facade.EffectCoordinator
)

for method_name, expected in spec["type_hint_identities"].items():
    hints = typing.get_type_hints(getattr(facade.EffectCoordinator, method_name))
    for parameter, value_name in expected.items():
        assert hints[parameter] is getattr(values, value_name)
for name in ("ReconcileReport", "_Context", "_PublicationItemContext"):
    assert getattr(facade, name) is getattr(values, name)

instance = object.__new__(facade.EffectCoordinator)
assert type(instance.__dict__) is dict
assert vars(instance) is instance.__dict__
sentinel = object()
instance.__dict__["_boundary_sentinel"] = sentinel
assert instance._boundary_sentinel is sentinel
for base_type in base_types:
    dictionary = base_type.__dict__["__dict__"].__get__(
        instance, facade.EffectCoordinator
    )
    assert dictionary is instance.__dict__
assert vars(instance) == {"_boundary_sentinel": sentinel}
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, payload],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_coordinator_methods_have_one_owner_and_frozen_dispatch_bodies() -> None:
    facade = importlib.import_module("lockstep.runtime.effects.coordinator")
    owners = [facade.EffectCoordinator]
    owners.extend(
        getattr(importlib.import_module(module_name), class_name)
        for module_name, (class_name, _methods) in _BASES.items()
    )
    nodes_by_owner = [_method_nodes(owner) for owner in owners]
    counts = Counter(name for nodes in nodes_by_owner for name in nodes)
    assert set(counts) == set(_EXPECTED_METHOD_DIGESTS) | _NEW_HELPERS
    assert set(counts.values()) == {1}

    actual_nodes = {
        name: node for nodes in nodes_by_owner for name, node in nodes.items()
    }
    frozen = {
        name: _digest(actual_nodes[name])
        for name in set(_EXPECTED_METHOD_DIGESTS) - _DECOMPOSED_METHODS
    }
    assert frozen == {
        name: digest
        for name, digest in _EXPECTED_METHOD_DIGESTS.items()
        if name not in _DECOMPOSED_METHODS
    }
    assert len(frozen) == 84
    assert all(
        _digest(actual_nodes[name]) != _EXPECTED_METHOD_DIGESTS[name]
        for name in _DECOMPOSED_METHODS
    )

    field_writers = {
        name: {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        for nodes in nodes_by_owner
        for name, method in nodes.items()
    }
    assert field_writers["__init__"] == _INSTANCE_FIELDS
    assert all(not fields for name, fields in field_writers.items() if name != "__init__")


def test_decomposed_transactions_preserve_exact_guard_and_commit_order() -> None:
    method_nodes = {
        name: node
        for module_name, (class_name, _methods) in _BASES.items()
        for name, node in _method_nodes(
            getattr(importlib.import_module(module_name), class_name)
        ).items()
    }

    def relevant(name: str, allowed: set[str]) -> tuple[str, ...]:
        return tuple(
            call for call in _ordered_calls(method_nodes[name]) if call in allowed
        )

    def call_node(name: str, label: str) -> ast.Call:
        return next(
            node
            for node in ast.walk(method_nodes[name])
            if isinstance(node, ast.Call) and ast.unparse(node.func) == label
        )

    assert relevant(
        "_reconcile_context",
        {
            "self._context",
            "self._authority_blocked_observation",
            "self._commit_authority_blocked_observation",
        },
    ) == (
        "self._context",
        "self._authority_blocked_observation",
        "self._commit_authority_blocked_observation",
    )
    context_handlers = [
        node
        for node in ast.walk(method_nodes["_reconcile_context"])
        if isinstance(node, ast.ExceptHandler)
    ]
    assert len(context_handlers) == 1
    assert ast.unparse(context_handlers[0].type) == (
        "(EffectAuthorityDenied, EffectAuthorityUnavailable)"
    )
    assert any(
        isinstance(node, ast.Raise) and node.exc is None
        for node in ast.walk(context_handlers[0])
    )
    assert relevant(
        "_authority_blocked_observation",
        {"self._runner_for_binding", "runner.inspect", "self._check_observation"},
    ) == (
        "self._runner_for_binding",
        "runner.inspect",
        "self._check_observation",
    )
    assert relevant(
        "_commit_authority_blocked_observation",
        {
            "self._report",
            "self._ledger.mark_indeterminate",
            "self._ledger.mark_running",
        },
    ) == (
        "self._report",
        "self._ledger.mark_indeterminate",
        "self._report",
        "self._ledger.mark_running",
        "self._report",
    )

    assert relevant(
        "_commit_publication_recovery",
        {
            "self._publication_lease",
            "self._report",
            "self._guarded_publication_recovery",
            "self._finalize_publication_recovery",
            "self._leases.release",
        },
    ) == (
        "self._publication_lease",
        "self._report",
        "self._guarded_publication_recovery",
        "self._finalize_publication_recovery",
        "self._leases.release",
    )
    recovery_try = next(
        node
        for node in method_nodes["_commit_publication_recovery"].body
        if isinstance(node, ast.Try)
    )
    assert relevant(
        "_guarded_publication_recovery",
        {
            "self._runtime.commitment_guard",
            "parse_effect_descriptor",
            "self._raw_descriptor",
            "self._ledger.get",
            "self._publication_recovery_guard_is_current",
            "self._report",
            "self._publication_recovery_receipt",
        },
    ) == (
        "self._runtime.commitment_guard",
        "parse_effect_descriptor",
        "self._raw_descriptor",
        "self._ledger.get",
        "self._publication_recovery_guard_is_current",
        "self._report",
        "self._publication_recovery_receipt",
    )
    assert relevant(
        "_publication_recovery_receipt",
        {
            "publisher.rollback_or_recover",
            "self._publication_error_result",
            "publisher.apply_or_recover",
            "self._publication_result",
        },
    ) == (
        "publisher.rollback_or_recover",
        "self._publication_error_result",
        "publisher.apply_or_recover",
        "self._publication_result",
    )
    assert relevant(
        "_publication_recovery_guard_is_current",
        {"self._leases.is_current"},
    ) == ("self._leases.is_current", "self._leases.is_current")
    assert relevant(
        "_finalize_publication_recovery",
        {
            "self._report",
            "self._capture_publication_successor",
            "self._ledger.seal",
        },
    ) == (
        "self._report",
        "self._capture_publication_successor",
        "self._ledger.seal",
        "self._report",
    )
    assert _ordered_calls(recovery_try.finalbody[0]) == ("self._leases.release",)
    recovery_seal = call_node(
        "_finalize_publication_recovery", "self._ledger.seal"
    )
    assert {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in recovery_seal.keywords
    } == {
        "expected_revision": "record.revision",
        "lease": "lease",
        "runner_binding_digest": "publisher.binding_digest",
    }

    assert relevant(
        "_reconcile_publication",
        {
            "self._publisher_for",
            "self._publication_existing_result",
            "self._publication_intent",
            "self._prepare_publication_effect",
            "self._validate_publication_authority",
            "self._prepared_publication_commitment",
            "self._advance_publication_effect",
        },
    ) == (
        "self._publisher_for",
        "self._publication_existing_result",
        "self._publication_intent",
        "self._prepare_publication_effect",
        "self._validate_publication_authority",
        "self._prepared_publication_commitment",
        "self._advance_publication_effect",
    )

    required_strings = {
        "authority_blocked",
        "indeterminate",
        "running",
        "unknown launch observation state",
        "publication_progress",
        "sealed",
        "awaiting_delivery",
        "prepared",
        "publication_claimed",
        "publication authority differs from durable ledger facts",
        "publication journal differs from durable commitment",
        "publication has an impossible ledger phase",
    }
    decomposition_nodes = tuple(
        method_nodes[name] for name in _DECOMPOSED_METHODS | _NEW_HELPERS
    )
    actual_strings = {
        node.value
        for method in decomposition_nodes
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert required_strings <= actual_strings

    assert relevant(
        "_commit_prepared_publication",
        {"self._authority.commitment", "publisher.apply_or_recover"},
    ) == ("self._authority.commitment", "publisher.apply_or_recover")
    launching = call_node("_advance_publication_effect", "self._ledger.mark_launching")
    assert {
        keyword.arg: ast.unparse(keyword.value) for keyword in launching.keywords
    } == {
        "expected_revision": "record.revision",
        "lease": "lease",
        "runner_binding_digest": "publisher.binding_digest",
        "launch_commitment_digest": "commitment_digest",
    }
    assert relevant(
        "_commit_deliverable_records",
        {"self._runtime.resume", "self._ledger.mark_delivered"},
    ) == ("self._runtime.resume", "self._ledger.mark_delivered")


def test_decomposed_authority_recovery_preserves_exact_branches_and_exceptions() -> None:
    importlib.import_module("lockstep.runtime.effects._coordinator_authority_recovery")
    from lockstep.runtime.effects.authority import EffectAuthorityDenied
    from lockstep.runtime.effects.coordinator import (
        CoordinatorLineageError,
        EffectCoordinator,
        ProviderContractViolation,
    )
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    coordinator = object.__new__(EffectCoordinator)
    descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "managed",
            "logical_id": "boundary",
            "runner": {
                "selector": "codex",
                "required_capabilities": ["workspace", "bounded_result"],
            },
            "inputs": {},
            "writes": ["src/"],
            "artifacts": [],
            "deadline_seconds": 30,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )
    record = SimpleNamespace(
        effect_id="effect-1",
        phase="launching",
        revision=7,
        deadline_at=None,
        runner_binding_digest="runner-digest",
    )
    lease = object()
    binding = snapshot = interrupt = object()
    observation = SimpleNamespace(state="running")
    expected = object()
    calls: list[tuple[object, ...]] = []
    denied = EffectAuthorityDenied("exact denial")

    def deny_context(*args, **kwargs):
        calls.append(("context", args, kwargs))
        raise denied

    def blocked_observation(*, record):
        calls.append(("observation", record))
        return observation

    def commit_blocked(*, run_id, record, observation, lease):
        calls.append(("commit", run_id, record, observation, lease))
        return expected

    coordinator._context = deny_context
    coordinator._authority_blocked_observation = blocked_observation
    coordinator._commit_authority_blocked_observation = commit_blocked
    assert coordinator._reconcile_context(
        run_id="run-1",
        binding=binding,
        snapshot=snapshot,
        interrupt=interrupt,
        descriptor=descriptor,
        effect_id=record.effect_id,
        record=record,
        lease=lease,
    ) is expected
    assert calls == [
        (
            "context",
            (binding, snapshot, interrupt),
            {
                "descriptor": descriptor,
                "effect_id": record.effect_id,
                "record": record,
                "resolve_grant": True,
            },
        ),
        ("observation", record),
        ("commit", "run-1", record, observation, lease),
    ]

    calls.clear()
    with pytest.raises(EffectAuthorityDenied) as caught:
        coordinator._reconcile_context(
            run_id="run-1",
            binding=binding,
            snapshot=snapshot,
            interrupt=interrupt,
            descriptor=descriptor,
            effect_id="new-effect",
            record=None,
            lease=lease,
        )
    assert caught.value is denied
    assert calls == [
        (
            "context",
            (binding, snapshot, interrupt),
            {
                "descriptor": descriptor,
                "effect_id": "new-effect",
                "record": None,
                "resolve_grant": True,
            },
        )
    ]

    del coordinator._authority_blocked_observation
    del coordinator._commit_authority_blocked_observation
    runner_calls: list[tuple[object, ...]] = []
    runner = SimpleNamespace(
        inspect=lambda effect_id: (
            runner_calls.append(("inspect", effect_id)) or observation
        )
    )
    coordinator._runner_for_binding = lambda digest: (
        runner_calls.append(("runner", digest)) or runner
    )
    coordinator._check_observation = lambda durable, observed: runner_calls.append(
        ("check", durable, observed)
    )
    assert coordinator._authority_blocked_observation(record=record) is observation
    assert runner_calls == [
        ("runner", "runner-digest"),
        ("inspect", "effect-1"),
        ("check", record, observation),
    ]

    ledger_calls: list[tuple[object, ...]] = []
    marked_indeterminate = object()
    marked_running = object()
    coordinator._ledger = SimpleNamespace(
        mark_indeterminate=lambda *args, **kwargs: (
            ledger_calls.append(("indeterminate", args, kwargs))
            or marked_indeterminate
        ),
        mark_running=lambda *args, **kwargs: (
            ledger_calls.append(("running", args, kwargs)) or marked_running
        ),
    )
    coordinator._report = lambda run_id, durable, action: (
        "report", run_id, durable, action
    )
    assert coordinator._commit_authority_blocked_observation(
        run_id="run-1",
        record=record,
        observation=SimpleNamespace(state="absent"),
        lease=lease,
    ) == ("report", "run-1", record, "authority_blocked")
    assert ledger_calls == []
    assert coordinator._commit_authority_blocked_observation(
        run_id="run-1",
        record=record,
        observation=SimpleNamespace(state="indeterminate"),
        lease=lease,
    ) == ("report", "run-1", marked_indeterminate, "indeterminate")
    assert ledger_calls.pop(0) == (
        "indeterminate",
        ("effect-1",),
        {"expected_revision": 7, "lease": lease},
    )
    for state in ("running", "terminal"):
        assert coordinator._commit_authority_blocked_observation(
            run_id="run-1",
            record=record,
            observation=SimpleNamespace(state=state),
            lease=lease,
        ) == ("report", "run-1", marked_running, "running")
        assert ledger_calls.pop(0) == (
            "running",
            ("effect-1",),
            {
                "expected_revision": 7,
                "lease": lease,
                "runner_binding_digest": "runner-digest",
            },
        )
    with pytest.raises(ProviderContractViolation, match="unknown launch observation state"):
        coordinator._commit_authority_blocked_observation(
            run_id="run-1",
            record=record,
            observation=SimpleNamespace(state="unknown"),
            lease=lease,
        )
    assert ledger_calls == []
    assert CoordinatorLineageError.__module__ == "lockstep.runtime.effects._coordinator_values"


def test_reconcile_context_rejects_every_ineligible_authority_recovery_path() -> None:
    importlib.import_module("lockstep.runtime.effects._coordinator_authority_recovery")
    from lockstep.runtime.effects.authority import (
        EffectAuthorityDenied,
        EffectAuthorityUnavailable,
    )
    from lockstep.runtime.effects.coordinator import (
        EffectCoordinator,
        ProviderContractViolation,
    )
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    coordinator = object.__new__(EffectCoordinator)
    managed = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "managed",
            "logical_id": "managed-boundary",
            "runner": {
                "selector": "codex",
                "required_capabilities": ["workspace", "bounded_result"],
            },
            "inputs": {},
            "writes": ["src/"],
            "artifacts": [],
            "deadline_seconds": 30,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )
    manual = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": "manual-boundary",
            "runner": None,
            "inputs": {},
            "writes": ["src/"],
            "artifacts": [],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
    )
    scope = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "scope",
            "logical_id": "scope-boundary",
            "scope_kind": "parallel",
            "duration_seconds": 30,
            "runner_selector": None,
            "ancestor_deadline_state_keys": [],
            "result_state_key": "scope_result",
            "result_schema": "lockstep.scope-result/v1",
        }
    )
    launching = SimpleNamespace(
        effect_id="effect-1",
        phase="launching",
        revision=3,
        deadline_at=None,
        runner_binding_digest="runner-digest",
    )
    cases = (
        (SimpleNamespace(**{**vars(launching), "phase": "prepared"}), managed),
        (launching, scope),
        (launching, manual),
    )
    for error_type in (EffectAuthorityDenied, EffectAuthorityUnavailable):
        for record, descriptor in cases:
            error = error_type("exact authority failure")
            calls: list[object] = []

            def fail_context(*args, _calls=calls, _error=error, **kwargs):
                _calls.append(("context", args, kwargs))
                raise _error

            coordinator._context = fail_context
            coordinator._authority_blocked_observation = (
                lambda _calls=calls, **kwargs: _calls.append(("observation", kwargs))
            )
            coordinator._commit_authority_blocked_observation = (
                lambda _calls=calls, **kwargs: _calls.append(("commit", kwargs))
            )
            with pytest.raises(error_type) as caught:
                coordinator._reconcile_context(
                    run_id="run-1",
                    binding=object(),
                    snapshot=object(),
                    interrupt=object(),
                    descriptor=descriptor,
                    effect_id=record.effect_id,
                    record=record,
                    lease=object(),
                )
            assert caught.value is error
            assert [item[0] for item in calls] == ["context"]

    denied = EffectAuthorityDenied("eligible denial")
    contract_error = ProviderContractViolation("exact observation error")
    later_calls: list[object] = []

    def deny(*_args, **_kwargs):
        raise denied

    def reject_observation(**kwargs):
        later_calls.append(("observation", kwargs))
        raise contract_error

    coordinator._context = deny
    coordinator._authority_blocked_observation = reject_observation
    coordinator._commit_authority_blocked_observation = lambda **kwargs: later_calls.append(
        ("commit", kwargs)
    )
    with pytest.raises(ProviderContractViolation) as caught:
        coordinator._reconcile_context(
            run_id="run-1",
            binding=object(),
            snapshot=object(),
            interrupt=object(),
            descriptor=managed,
            effect_id=launching.effect_id,
            record=launching,
            lease=object(),
        )
    assert caught.value is contract_error
    assert [item[0] for item in later_calls] == ["observation"]


@pytest.mark.parametrize(
    ("mismatch", "expected_lease_checks"),
    (
        ("binding", 0),
        ("coordinate", 0),
        ("descriptor_digest", 0),
        ("revision", 0),
        ("phase", 0),
        ("effect_lease", 1),
        ("publication_lease", 2),
    ),
)
def test_publication_recovery_guard_rejects_each_fact_independently(
    mismatch: str, expected_lease_checks: int
) -> None:
    importlib.import_module(
        "lockstep.runtime.effects._coordinator_publication_recovery_policy"
    )
    from lockstep.runtime.effects.coordinator import EffectCoordinator

    coordinator = object.__new__(EffectCoordinator)
    binding = object()
    coordinate = object()
    descriptor = SimpleNamespace(digest="a" * 64)
    guarded = SimpleNamespace(
        binding=binding,
        interrupt=SimpleNamespace(coordinate=coordinate),
    )
    guarded_descriptor = SimpleNamespace(digest="a" * 64)
    record = SimpleNamespace(coordinate=coordinate, revision=7)
    current = SimpleNamespace(revision=7, phase="launching")
    lease = object()
    publication_lease = object()
    answers = {lease: True, publication_lease: True}

    if mismatch == "binding":
        guarded.binding = object()
    elif mismatch == "coordinate":
        guarded.interrupt = SimpleNamespace(coordinate=object())
    elif mismatch == "descriptor_digest":
        guarded_descriptor = SimpleNamespace(digest="b" * 64)
    elif mismatch == "revision":
        current.revision = 8
    elif mismatch == "phase":
        current.phase = "prepared"
    elif mismatch == "effect_lease":
        answers[lease] = False
    elif mismatch == "publication_lease":
        answers[publication_lease] = False
    else:
        raise AssertionError(mismatch)

    calls: list[object] = []
    coordinator._leases = SimpleNamespace(
        is_current=lambda value: calls.append(value) or answers[value]
    )
    assert coordinator._publication_recovery_guard_is_current(
        guarded=guarded,
        binding=binding,
        guarded_descriptor=guarded_descriptor,
        descriptor=descriptor,
        current=current,
        record=record,
        lease=lease,
        publication_lease=publication_lease,
    ) is False
    assert calls == [lease, publication_lease][:expected_lease_checks]


def test_decomposed_publication_recovery_preserves_guard_dataflow_and_finally() -> None:
    importlib.import_module(
        "lockstep.runtime.effects._coordinator_publication_recovery_transaction"
    )
    from lockstep.runtime.effects.coordinator import EffectCoordinator
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    coordinator = object.__new__(EffectCoordinator)
    raw_descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": "publish-boundary",
        "items": [{
            "qualified_handle": "call.review",
            "producer_result_state_key": "producer_result",
            "declared_name": "review",
            "acceptance_result_state_key": "acceptance_result",
            "destination": ".lockstep/review.md",
            "transformation": "identity",
            "audience": "local-project",
        }],
        "result_schema": "lockstep.effect-result/v1",
    }
    descriptor = parse_effect_descriptor(raw_descriptor)
    binding = object()
    coordinate = object()
    interrupt = SimpleNamespace(coordinate=coordinate)
    guarded = SimpleNamespace(binding=binding, interrupt=interrupt)
    record = SimpleNamespace(
        effect_id="effect-1", coordinate=coordinate, revision=9, phase="launching"
    )
    current = SimpleNamespace(revision=9, phase="launching")
    lease = object()
    publication_lease = object()

    for first, second, expected, expected_calls in (
        (True, True, True, [lease, publication_lease]),
        (True, False, False, [lease, publication_lease]),
        (False, True, False, [lease]),
    ):
        calls: list[object] = []
        answers = {lease: first, publication_lease: second}
        coordinator._leases = SimpleNamespace(
            is_current=lambda item, _calls=calls, _answers=answers: (
                _calls.append(item) or _answers[item]
            )
        )
        assert coordinator._publication_recovery_guard_is_current(
            guarded=guarded,
            binding=binding,
            guarded_descriptor=descriptor,
            descriptor=descriptor,
            current=current,
            record=record,
            lease=lease,
            publication_lease=publication_lease,
        ) is expected
        assert calls == expected_calls

    trace: list[object] = []

    @contextmanager
    def commitment_guard(run_id, source):
        trace.append(("guard-enter", run_id, source))
        try:
            yield guarded
        finally:
            trace.append(("guard-exit", run_id, source))

    receipt = SimpleNamespace(phase="applied", journal_digest="a" * 64)
    publisher = SimpleNamespace(
        apply_or_recover=lambda prepared: (
            trace.append(("apply", prepared)) or receipt
        ),
        rollback_or_recover=lambda prepared: (
            trace.append(("rollback", prepared)) or receipt
        ),
    )
    prepared_publication = object()
    coordinator._runtime = SimpleNamespace(commitment_guard=commitment_guard)
    coordinator._raw_descriptor = lambda value: (
        trace.append(("raw", value)) or raw_descriptor
    )
    coordinator._ledger = SimpleNamespace(
        get=lambda effect_id: trace.append(("get", effect_id)) or current
    )
    coordinator._publication_recovery_guard_is_current = lambda **kwargs: (
        trace.append(("current", kwargs)) or True
    )
    recovered = coordinator._guarded_publication_recovery(
        run_id="run-1",
        binding=binding,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publication_lease=publication_lease,
        publisher=publisher,
        prepared_publication=prepared_publication,
        recovery_phase="applying",
    )
    recovered_receipt, recovered_result = recovered
    assert recovered_receipt is receipt
    assert recovered_result.effect_id == "effect-1"
    assert recovered_result.outcome == "PASS"
    assert [item[0] for item in trace] == [
        "guard-enter", "raw", "get", "current", "apply", "guard-exit"
    ]
    assert trace[2] == ("get", "effect-1")
    guard_kwargs = trace[3][1]
    assert guard_kwargs == {
        "guarded": guarded,
        "binding": binding,
        "guarded_descriptor": descriptor,
        "descriptor": descriptor,
        "current": current,
        "record": record,
        "lease": lease,
        "publication_lease": publication_lease,
    }

    trace.clear()
    coordinator._report = lambda run_id, durable, action: (
        "report", run_id, durable, action
    )
    coordinator._publication_recovery_guard_is_current = lambda **kwargs: (
        trace.append(("current", kwargs)) or False
    )
    assert coordinator._guarded_publication_recovery(
        run_id="run-1",
        binding=binding,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publication_lease=publication_lease,
        publisher=publisher,
        prepared_publication=prepared_publication,
        recovery_phase="applying",
    ) == ("report", "run-1", current, "busy")
    assert [item[0] for item in trace] == [
        "guard-enter", "raw", "get", "current", "guard-exit"
    ]

    rollback_receipt = SimpleNamespace(phase="rolled_back", journal_digest="b" * 64)
    rollback_calls: list[object] = []
    publisher.rollback_or_recover = lambda prepared: (
        rollback_calls.append(prepared) or rollback_receipt
    )
    recovered_receipt, recovered_result = coordinator._publication_recovery_receipt(
        publisher=publisher,
        prepared_publication=prepared_publication,
        recovery_phase="rollback_pending",
        effect_id="effect-1",
    )
    assert recovered_receipt is rollback_receipt
    assert recovered_result.outcome == "ERROR"
    assert recovered_result.fixed_error_code == "provider_error"
    assert rollback_calls == [prepared_publication]

    reports: list[tuple[object, ...]] = []
    coordinator._report = lambda run_id, durable, action: (
        reports.append((run_id, durable, action)) or reports[-1]
    )
    capture_calls: list[dict[str, object]] = []
    coordinator._capture_publication_successor = lambda **kwargs: capture_calls.append(kwargs)
    sealed = object()
    seal_calls: list[tuple[object, ...]] = []
    coordinator._ledger = SimpleNamespace(
        seal=lambda *args, **kwargs: seal_calls.append((args, kwargs)) or sealed
    )
    publisher.binding_digest = "publisher-digest"
    assert coordinator._finalize_publication_recovery(
        run_id="run-1",
        binding=binding,
        interrupt=interrupt,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publisher=publisher,
        receipt=receipt,
        result=recovered_result,
    ) == ("run-1", sealed, "sealed")
    assert capture_calls == [{
        "binding": binding,
        "interrupt": interrupt,
        "descriptor": descriptor,
        "effect_id": "effect-1",
    }]
    assert seal_calls == [(('effect-1', recovered_result), {
        "expected_revision": 9,
        "lease": lease,
        "runner_binding_digest": "publisher-digest",
    })]

    progress = SimpleNamespace(phase="applying")
    capture_calls.clear()
    seal_calls.clear()
    assert coordinator._finalize_publication_recovery(
        run_id="run-1",
        binding=binding,
        interrupt=interrupt,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publisher=publisher,
        receipt=progress,
        result=recovered_result,
    ) == ("run-1", record, "publication_progress")
    assert capture_calls == seal_calls == []

    capture_calls.clear()
    seal_calls.clear()
    assert coordinator._finalize_publication_recovery(
        run_id="run-1",
        binding=binding,
        interrupt=interrupt,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publisher=publisher,
        receipt=rollback_receipt,
        result=recovered_result,
    ) == ("run-1", sealed, "sealed")
    assert capture_calls == []
    assert len(seal_calls) == 1

    shell_trace: list[object] = []
    coordinator._report = lambda run_id, durable, action: (
        "report", run_id, durable, action
    )
    coordinator._publication_lease = lambda value: None
    coordinator._leases = SimpleNamespace(
        release=lambda value: shell_trace.append(("release", value))
    )
    assert coordinator._commit_publication_recovery(
        run_id="run-1",
        binding=binding,
        interrupt=interrupt,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publisher=publisher,
        prepared_publication=prepared_publication,
        recovery_phase="applying",
    ) == ("report", "run-1", record, "busy")
    assert shell_trace == []

    coordinator._publication_lease = lambda value: (
        shell_trace.append(("acquire", value)) or publication_lease
    )
    pair = (receipt, recovered_result)
    coordinator._guarded_publication_recovery = lambda **kwargs: (
        shell_trace.append(("guarded", kwargs)) or pair
    )
    expected = object()
    coordinator._finalize_publication_recovery = lambda **kwargs: (
        shell_trace.append(("finalize", kwargs)) or expected
    )
    assert coordinator._commit_publication_recovery(
        run_id="run-1",
        binding=binding,
        interrupt=interrupt,
        descriptor=descriptor,
        record=record,
        lease=lease,
        publisher=publisher,
        prepared_publication=prepared_publication,
        recovery_phase="applying",
    ) is expected
    assert [item[0] for item in shell_trace] == [
        "acquire", "guarded", "finalize", "release"
    ]
    assert shell_trace[-1] == ("release", publication_lease)

    failure = RuntimeError("recovery failed")
    shell_trace.clear()

    def fail_guarded(**_kwargs):
        shell_trace.append(("guarded",))
        raise failure

    coordinator._guarded_publication_recovery = fail_guarded
    with pytest.raises(RuntimeError) as caught:
        coordinator._commit_publication_recovery(
            run_id="run-1",
            binding=binding,
            interrupt=interrupt,
            descriptor=descriptor,
            record=record,
            lease=lease,
            publisher=publisher,
            prepared_publication=prepared_publication,
            recovery_phase="applying",
        )
    assert caught.value is failure
    assert shell_trace == [
        ("acquire", binding), ("guarded",), ("release", publication_lease)
    ]


def test_decomposed_publication_transition_preserves_exact_durable_facts() -> None:
    importlib.import_module("lockstep.runtime.effects._coordinator_publication_transition")
    from lockstep.runtime.effects.coordinator import (
        CoordinatorLineageError,
        EffectCoordinator,
    )

    coordinator = object.__new__(EffectCoordinator)
    lease = object()
    publisher = SimpleNamespace(binding_digest="publisher-digest")
    request = SimpleNamespace(request_digest="request-digest")
    grant = SimpleNamespace(digest="grant-digest")
    record = SimpleNamespace(
        effect_id="effect-1",
        coordinate=object(),
        phase="prepared",
        revision=11,
        request_digest="request-digest",
        grant_digest="grant-digest",
        runner_binding_digest="publisher-digest",
        launch_commitment_digest=None,
    )
    assert coordinator._validate_publication_authority(
        record=record, request=request, grant=grant, publisher=publisher
    ) is None
    for field, wrong in (
        ("request_digest", "wrong-request"),
        ("grant_digest", "wrong-grant"),
        ("runner_binding_digest", "wrong-publisher"),
    ):
        durable = SimpleNamespace(**vars(record))
        setattr(durable, field, wrong)
        with pytest.raises(
            CoordinatorLineageError,
            match="publication authority differs from durable ledger facts",
        ):
            coordinator._validate_publication_authority(
                record=durable, request=request, grant=grant, publisher=publisher
            )

    publication_request = object()
    prepared_publication = object()
    prepare_calls: list[object] = []
    publisher.prepare = lambda value: prepare_calls.append(("prepare", value)) or prepared_publication
    publisher.commitment_digest = lambda value: (
        prepare_calls.append(("digest", value)) or "commitment-digest"
    )
    assert coordinator._prepared_publication_commitment(
        publisher=publisher, publication_request=publication_request
    ) == (prepared_publication, "commitment-digest")
    assert prepare_calls == [
        ("prepare", publication_request), ("digest", prepared_publication)
    ]

    reports: list[tuple[object, ...]] = []
    coordinator._report = lambda run_id, durable, action: (
        reports.append((run_id, durable, action)) or reports[-1]
    )
    claimed = object()
    ledger_calls: list[tuple[object, ...]] = []
    coordinator._ledger = SimpleNamespace(
        mark_launching=lambda *args, **kwargs: (
            ledger_calls.append((args, kwargs)) or claimed
        )
    )
    assert coordinator._advance_publication_effect(
        run_id="run-1",
        binding=object(),
        interrupt=object(),
        descriptor=object(),
        record=record,
        lease=lease,
        publisher=publisher,
        request=request,
        grant=grant,
        prepared_publication=prepared_publication,
        commitment_digest="commitment-digest",
    ) == ("run-1", claimed, "publication_claimed")
    assert ledger_calls == [(('effect-1',), {
        "expected_revision": 11,
        "lease": lease,
        "runner_binding_digest": "publisher-digest",
        "launch_commitment_digest": "commitment-digest",
    })]

    launching = SimpleNamespace(**vars(record))
    launching.phase = "launching"
    launching.launch_commitment_digest = "wrong-digest"
    with pytest.raises(
        CoordinatorLineageError,
        match="publication journal differs from durable commitment",
    ):
        coordinator._advance_publication_effect(
            run_id="run-1", binding=object(), interrupt=object(), descriptor=object(),
            record=launching, lease=lease, publisher=publisher, request=request,
            grant=grant, prepared_publication=prepared_publication,
            commitment_digest="commitment-digest",
        )
    coordinator._commit_prepared_publication = lambda **kwargs: kwargs
    launching.launch_commitment_digest = "commitment-digest"
    committed = coordinator._advance_publication_effect(
        run_id="run-1", binding="binding", interrupt="interrupt",
        descriptor="descriptor", record=launching, lease=lease,
        publisher=publisher, request=request, grant=grant,
        prepared_publication=prepared_publication,
        commitment_digest="commitment-digest",
    )
    assert committed == {
        "run_id": "run-1", "binding": "binding", "interrupt": "interrupt",
        "descriptor": "descriptor", "record": launching, "lease": lease,
        "publisher": publisher, "request": request, "grant": grant,
        "prepared_publication": prepared_publication,
    }
    impossible = SimpleNamespace(**vars(record))
    impossible.phase = "running"
    with pytest.raises(
        CoordinatorLineageError, match="publication has an impossible ledger phase"
    ):
        coordinator._advance_publication_effect(
            run_id="run-1", binding=object(), interrupt=object(), descriptor=object(),
            record=impossible, lease=lease, publisher=publisher, request=request,
            grant=grant, prepared_publication=prepared_publication,
            commitment_digest="commitment-digest",
        )

    coordinate = object()
    descriptor = object()
    interrupt = SimpleNamespace(coordinate=coordinate)
    prepared_record = object()
    prepare_ledger_calls: list[tuple[object, ...]] = []
    coordinator._ledger = SimpleNamespace(
        prepare=lambda *args, **kwargs: (
            prepare_ledger_calls.append((args, kwargs)) or prepared_record
        )
    )
    assert coordinator._prepare_publication_effect(
        run_id="run-1", interrupt=interrupt, descriptor=descriptor,
        request=request, grant=grant, publisher=publisher, lease=lease,
    ) == ("run-1", prepared_record, "prepared")
    assert prepare_ledger_calls == [((coordinate, descriptor), {
        "deadline_at": None,
        "runner_binding_digest": "publisher-digest",
        "workspace_ref": None,
        "request_digest": "request-digest",
        "grant_digest": "grant-digest",
        "lease": lease,
    })]

    sealed = SimpleNamespace(phase="sealed")
    assert coordinator._publication_existing_result(
        run_id="run-1", binding=object(), interrupt=interrupt,
        descriptor=descriptor, record=sealed, lease=lease, publisher=publisher,
    ) == ("run-1", sealed, "awaiting_delivery")
    recovered = object()
    launching.phase = "launching"
    coordinator._recover_publication = lambda **kwargs: recovered
    assert coordinator._publication_existing_result(
        run_id="run-1", binding="binding", interrupt=interrupt,
        descriptor=descriptor, record=launching, lease=lease, publisher=publisher,
    ) is recovered
    assert coordinator._publication_existing_result(
        run_id="run-1", binding=object(), interrupt=interrupt,
        descriptor=descriptor, record=None, lease=lease, publisher=publisher,
    ) is None

    orchestration: list[tuple[object, ...]] = []
    binding = snapshot = object()
    effect_id = "effect-1"
    coordinator._publisher_for = lambda value: (
        orchestration.append(("publisher", value)) or publisher
    )
    coordinator._publication_existing_result = lambda **kwargs: (
        orchestration.append(("existing", kwargs)) or None
    )
    coordinator._publication_intent = lambda *args: (
        orchestration.append(("intent", args))
        or (request, grant, publication_request)
    )
    coordinator._validate_publication_authority = lambda **kwargs: orchestration.append(
        ("validate", kwargs)
    )
    coordinator._prepared_publication_commitment = lambda **kwargs: (
        orchestration.append(("commitment", kwargs))
        or (prepared_publication, "commitment-digest")
    )
    advanced = object()
    coordinator._advance_publication_effect = lambda **kwargs: (
        orchestration.append(("advance", kwargs)) or advanced
    )
    assert coordinator._reconcile_publication(
        "run-1", binding, snapshot, descriptor, interrupt, effect_id, record, lease
    ) is advanced
    assert [item[0] for item in orchestration] == [
        "publisher", "existing", "intent", "validate", "commitment", "advance"
    ]
    assert orchestration[1][1] == {
        "run_id": "run-1", "binding": binding, "interrupt": interrupt,
        "descriptor": descriptor, "record": record, "lease": lease,
        "publisher": publisher,
    }
    assert orchestration[2][1] == (
        binding, snapshot, interrupt, descriptor, effect_id, publisher
    )
    assert orchestration[3][1] == {
        "record": record, "request": request, "grant": grant,
        "publisher": publisher,
    }
    assert orchestration[4][1] == {
        "publisher": publisher, "publication_request": publication_request,
    }
    assert orchestration[5][1] == {
        "run_id": "run-1", "binding": binding, "interrupt": interrupt,
        "descriptor": descriptor, "record": record, "lease": lease,
        "publisher": publisher, "request": request, "grant": grant,
        "prepared_publication": prepared_publication,
        "commitment_digest": "commitment-digest",
    }

    orchestration.clear()
    early = object()
    coordinator._publication_existing_result = lambda **kwargs: (
        orchestration.append(("existing", kwargs)) or early
    )
    assert coordinator._reconcile_publication(
        "run-1", binding, snapshot, descriptor, interrupt, effect_id, record, lease
    ) is early
    assert [item[0] for item in orchestration] == ["publisher", "existing"]

    orchestration.clear()
    coordinator._publication_existing_result = lambda **kwargs: (
        orchestration.append(("existing", kwargs)) or None
    )
    prepared_report = object()
    coordinator._prepare_publication_effect = lambda **kwargs: (
        orchestration.append(("prepare-effect", kwargs)) or prepared_report
    )
    assert coordinator._reconcile_publication(
        "run-1", binding, snapshot, descriptor, interrupt, effect_id, None, lease
    ) is prepared_report
    assert [item[0] for item in orchestration] == [
        "publisher", "existing", "intent", "prepare-effect"
    ]
    assert orchestration[-1][1] == {
        "run_id": "run-1", "interrupt": interrupt, "descriptor": descriptor,
        "request": request, "grant": grant, "publisher": publisher, "lease": lease,
    }


def test_coordinator_owner_dependency_graph_is_exact_and_acyclic() -> None:
    role_nodes: dict[str, dict[str, ast.AST]] = {}
    owner_by_method: dict[str, str] = {}
    for module_name, (class_name, expected_methods) in _BASES.items():
        role = module_name.rsplit("_coordinator_", 1)[1]
        assert role not in {"common", "misc"}
        nodes = _method_nodes(getattr(importlib.import_module(module_name), class_name))
        assert set(nodes) == set(expected_methods)
        role_nodes[role] = nodes
        owner_by_method.update((name, role) for name in nodes)

    edges = {
        (owner, owner_by_method[node.func.attr])
        for owner, nodes in role_nodes.items()
        for method in nodes.values()
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr in owner_by_method
        and owner_by_method[node.func.attr] != owner
    }
    assert edges == _ALLOWED_OWNER_DEPENDENCIES

    remaining = set(role_nodes)
    while remaining:
        leaves = {
            owner
            for owner in remaining
            if not {dependency for source, dependency in edges if source == owner} & remaining
        }
        assert leaves, f"cyclic coordinator owner graph: {sorted(remaining)}"
        remaining -= leaves


def test_coordinator_projection_has_exact_non_candidate_analyzer_boundary() -> None:
    payload = json.dumps(
        {
            "value_module": _VALUE_MODULE,
            "value_definitions": sorted(_VALUE_DEFINITIONS),
            "facade_module": _FACADE_MODULE,
            "bases": [
                [module_name, class_name, list(methods)]
                for module_name, (class_name, methods) in _BASES.items()
            ],
            "instance_field_count": len(_INSTANCE_FIELDS),
            "allowed_one_hop_candidates": sorted(_ALLOWED_ONE_HOP_CANDIDATES),
            "decomposed_methods": sorted(_DECOMPOSED_METHODS),
            "new_helpers": sorted(_NEW_HELPERS),
            "existing_function_candidates": sorted(
                {
                    "_ancestor_results",
                    "_terminal_safety",
                    "_admit_artifacts",
                    "_recover_missing_effect",
                    "_commit_manual_submission",
                    "_acceptance_commitment",
                    "_commit_acceptance_submission",
                    "_deliverable_records",
                }
            ),
        },
        sort_keys=True,
    )
    script = r'''
import json
from pathlib import Path
import subprocess
import sys

spec = json.loads(sys.argv[1])
engine_root = Path.cwd()
architecture_root = engine_root / "tests" / "architecture"
sys.path.insert(0, str(architecture_root))
import test_no_god_methods as architecture


def source_path(module_name):
    return "src/" + module_name.replace(".", "/") + ".py"


paths = {
    "values": source_path(spec["value_module"]),
    "facade": source_path(spec["facade_module"]),
}
for module_name, _class_name, _methods in spec["bases"]:
    paths[module_name] = source_path(module_name)
for path in paths.values():
    assert (engine_root / path).is_file(), f"missing proposed coordinator module: {path}"

tracked = subprocess.run(
    ("git", "ls-files", "src/lockstep"),
    cwd=engine_root,
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()
source_paths = tuple(
    sorted(
        {path for path in tracked if path.endswith(".py")}
        | set(paths.values())
    )
)
files = {path: (engine_root / path).read_bytes() for path in source_paths}
index = architecture.build_source_index(engine_root, source_paths, files)
rule_names = {
    "allowlist": "architecture_effect_free_allowlist.json",
    "primitives": "architecture_effect_primitives.json",
    "lifecycle": "architecture_lifecycle.json",
    "schema": "architecture_metrics.schema.json",
    "thresholds": "architecture_thresholds.json",
}
rules = {
    name: json.loads((architecture_root / filename).read_bytes())
    for name, filename in rule_names.items()
}
resolutions = architecture.resolve_calls(
    index, rules["allowlist"], rules["primitives"]
)
semantics = architecture.propagate_semantics(
    index,
    resolutions,
    rules["primitives"],
    rules["lifecycle"],
    digest_inputs=architecture.domain_lifecycle.SemanticDigestInputs(
        architecture._canonical_sha256(rules["allowlist"]),
        architecture._canonical_sha256(rules["schema"]),
        architecture._canonical_sha256(rules["thresholds"]),
        "task-12c",
        "v1",
    ),
)
report = architecture.evaluate_candidates(
    index,
    architecture.measure_legacy_metrics(index),
    semantics,
    resolutions,
)

expected_file_shape = {
    paths["values"]: (len(spec["value_definitions"]), len(spec["value_definitions"])),
    paths["facade"]: (2, 1),
}
for module_name, _class_name, methods in spec["bases"]:
    expected_file_shape[paths[module_name]] = (len(methods) + 1, 1)
for path, shape in expected_file_shape.items():
    metric = report.files[f"{path}::@file"]
    assert (metric.definition_count, metric.class_count) == shape
    assert metric.hard_triggers == ()
    assert metric.candidate is False

expected_class_shape = {
    f'{paths["values"]}::{name}': (0, 0, 0)
    for name in spec["value_definitions"]
}
expected_class_shape[f'{paths["facade"]}::EffectCoordinator'] = (
    1,
    0,
    spec["instance_field_count"],
)
owner_for_method = {"__init__": (paths["facade"], "EffectCoordinator")}
for module_name, class_name, methods in spec["bases"]:
    path = paths[module_name]
    expected_class_shape[f"{path}::{class_name}"] = (
        len(methods),
        sum(not name.startswith("_") for name in methods),
        0,
    )
    owner_for_method.update((name, (path, class_name)) for name in methods)
for identity, shape in expected_class_shape.items():
    metric = report.classes[identity]
    assert (
        metric.method_count,
        metric.public_method_count,
        metric.mutable_field_count,
    ) == shape
    assert metric.hard_triggers == ()
    assert metric.candidate is False

expected_functions = {
    f"{path}::{class_name}.{method_name}"
    for method_name, (path, class_name) in owner_for_method.items()
}
proposed_prefixes = tuple(f"{path}::" for path in paths.values())
actual_functions = {
    identity
    for identity in report.functions
    if identity.startswith(proposed_prefixes)
}
assert actual_functions == expected_functions
expected_one_hops = {f"{identity}::@one_hop" for identity in expected_functions}
actual_one_hops = {
    identity
    for identity in report.one_hops
    if identity.startswith(proposed_prefixes)
}
assert actual_one_hops == expected_one_hops
assert all(report.one_hops[identity].hard_triggers == () for identity in expected_one_hops)
expected_one_hop_candidates = {
    f"{owner_for_method[name][0]}::{owner_for_method[name][1]}.{name}::@one_hop"
    for name in spec["allowed_one_hop_candidates"]
}
assert len(expected_one_hop_candidates) == 3
actual_one_hop_candidates = {
    identity
    for identity in expected_one_hops
    if report.one_hops[identity].candidate
}
assert actual_one_hop_candidates == expected_one_hop_candidates
for name in (
    "_context",
    "_reconcile_publication",
    "_reconcile_running_effect",
    "_reconcile_special_descriptor",
    "_reconcile_owned_effect",
    "reconcile",
):
    path, class_name = owner_for_method[name]
    identity = f"{path}::{class_name}.{name}::@one_hop"
    assert report.one_hops[identity].candidate is False

expected_function_candidates = {
    f"{owner_for_method[name][0]}::{owner_for_method[name][1]}.{name}"
    for name in spec["existing_function_candidates"]
}
assert len(expected_function_candidates) == 8
actual_function_candidates = {
    identity
    for identity in expected_functions
    if report.functions[identity].candidate
}
assert actual_function_candidates == expected_function_candidates
for name in (*spec["decomposed_methods"], *spec["new_helpers"]):
    path, class_name = owner_for_method[name]
    identity = f"{path}::{class_name}.{name}"
    assert report.functions[identity].hard_triggers == ()
    assert report.functions[identity].candidate is False
assert report.unresolved_callsites == ()
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, payload],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
