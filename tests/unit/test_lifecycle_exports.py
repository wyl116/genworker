# edition: baseline
from src.worker import lifecycle


def test_lifecycle_module_exports_skill_evolution_helpers():
    assert lifecycle.DutySkillDetector is not None
    assert lifecycle.run_duty_skill_detection is not None
    assert lifecycle.build_skill_from_payload is not None
    assert lifecycle.write_skill_md is not None
