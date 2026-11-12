"""敏感模型 __repr__ 脱敏（D53 评审项：token/secret 永不进日志/异常栈）。"""
from sqlalchemy.orm import Mapped

_SENSITIVE = ("token", "secret", "password", "passwd", "hash", "config_json")


def masked_repr(obj) -> str:
    """通用脱敏 repr：敏感列显示 <masked:N>，其余显示截断值。"""
    fields = []
    state = obj.__dict__ if not isinstance(obj, Mapped) else {}
    mapper = type(obj).__mapper__
    for col in mapper.columns:
        name = col.name
        if name == "id":
            continue
        val = getattr(obj, name, None)
        low = name.lower()
        if any(s in low for s in _SENSITIVE):
            shown = f"<masked:{len(str(val or ''))}>"
        else:
            shown = repr(str(val or "")[:40])
        fields.append(f"{name}={shown}")
    return f"{type(obj).__name__}(" + ", ".join(fields) + ")"
