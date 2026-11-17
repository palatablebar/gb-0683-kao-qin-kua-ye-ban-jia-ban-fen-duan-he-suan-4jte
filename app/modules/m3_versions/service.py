from app.modules.m3_versions.rules import apply
from app.modules.m3_versions.corrections import apply as apply_corrections

def execute(state):
    return apply_corrections(apply(state))
