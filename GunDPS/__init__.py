from __future__ import annotations

from typing import Any, NamedTuple

from mods_base import build_mod, hook, get_pc, BoolOption, SliderOption
import unrealsdk
from unrealsdk.hooks import Type
from unrealsdk.unreal import UObject, WrappedStruct, BoundFunction


# --- Options ---

assume_vanilla_bpds = BoolOption(
    "Assume Vanilla BPDs",
    False,
    description=(
        "When enabled, known vanilla weapon behaviours (e.g. Vladof launcher"
        " free shots, Torgue AR grenade barrels) are corrected in the DPS"
        " calculation and the (!) warning is removed."
    ),
)

assume_optimal_element = BoolOption(
    "Assume Optimal Element Target",
    False,
    description=(
        "Incendiary weapons are shown as if hitting flesh and corrosive"
        " weapons as if hitting armor, i.e. with the elemental damage bonus"
        " applied. Other weapons are unaffected."
    ),
)

normal_mode_element_bonus = BoolOption(
    "Normal Mode Element Bonus (1.5x)",
    False,
    description=(
        "Used by 'Assume Optimal Element Target'. Off = 1.75x bonus (True and"
        " Ultimate Vault Hunter Mode). On = 1.5x bonus (Normal Mode)."
    ),
)

show_dps_on_card = BoolOption(
    "Show DPS On Item Card",
    True,
    description="Add the DPS lines to the bonus text on weapon item cards.",
)

show_dps_comparison = BoolOption(
    "Show DPS Comparison",
    True,
    description=(
        "When a weapon is compared against another one, show the DPS"
        " difference on the item card: green (+) if higher, red (-) if lower."
    ),
)

show_dps_popup = BoolOption(
    "Show DPS Popup",
    True,
    description="Also show the DPS as a temporary popup when viewing a weapon.",
)

limit_jakobs_fire_rate = BoolOption(
    "Limit Jakobs Fire Rate",
    True,
    description=(
        "Jakobs pistols, assault rifles and shotguns fire once per trigger"
        " pull, so their listed fire rate is not achievable. When enabled,"
        " their DPS uses your own clicks per second (below) instead, if that"
        " is lower than the listed fire rate."
    ),
)

jakobs_clicks_per_second = SliderOption(
    "Jakobs Clicks Per Second",
    7,
    min_value=1,
    max_value=15,
    description="Estimated trigger pulls per second for Jakobs semi-automatic weapons.",
)

debug_logging = BoolOption(
    "Debug Logging",
    False,
    description=(
        "Write BPD / splash diagnostics to the console log each time a weapon"
        " card is shown. Leave off for normal use."
    ),
)

display_duration = SliderOption(
    "Display Duration",
    3,
    min_value=1,
    max_value=10,
    description="How long the DPS notification stays on screen (seconds).",
)


# --- Attribute Reading ---

# Maps friendly stat names to AttributeDefinition object paths.
# Resolved at runtime via unrealsdk.find_object("AttributeDefinition", path).
WEAPON_ATTRIBUTES: dict[str, str] = {
    # Core stats used by the default DPS formula
    "damage": "D_Attributes.Weapon.WeaponDamage",
    "fire_interval": "D_Attributes.Weapon.WeaponFireInterval",
    "mag_size": "D_Attributes.Weapon.WeaponClipSize",
    "reload_time": "D_Attributes.Weapon.WeaponReloadSpeed",
    "projectiles": "D_Attributes.Weapon.WeaponProjectilesPerShot",
    "shot_cost": "D_Attributes.Weapon.WeaponShotCost",
    # Additional stats available for custom formulas
    "spread": "D_Attributes.Weapon.WeaponSpread",
    "melee_damage": "D_Attributes.Weapon.WeaponMeleeDamage",
    "status_effect_damage": "D_Attributes.Weapon.WeaponStatusEffectDamage",
    "status_effect_chance": "D_Attributes.Weapon.WeaponBaseStatusEffectChanceModifier",
    "burst_count": "D_Attributes.Weapon.WeaponAutomaticBurstCount",
    "burst_interval": "D_Attributes.Weapon.WeaponBurstInterval",
    "extra_shot_chance": "D_Attributes.Weapon.WeaponExtraShotChance",
    "projectile_speed": "D_Attributes.Weapon.WeaponProjectileSpeedMultiplier",
    "ricochets": "D_Attributes.Weapon.WeaponAdditionalRicochets",
    "equip_time": "D_Attributes.Weapon.WeaponEquipTime",
    "zoom_fov": "D_Attributes.Weapon.WeaponZoomEndFOV",
    "knockback": "D_Attributes.Weapon.WeaponKnockback",
}

_attr_cache: dict[str, UObject | None] = {}


def _find_attr(key: str) -> UObject | None:
    if key not in _attr_cache:
        try:
            _attr_cache[key] = unrealsdk.find_object("AttributeDefinition", WEAPON_ATTRIBUTES[key])
        except Exception:
            _attr_cache[key] = None
    return _attr_cache[key]


def read_weapon_stat(weapon: UObject, key: str, default: float = 0.0) -> float:
    """Read a resolved attribute value from a weapon.

    Args:
        weapon:  The WillowWeapon UObject.
        key:     A key from WEAPON_ATTRIBUTES (e.g. "damage", "fire_interval").
        default: Returned when the attribute cannot be found or read.
    """
    attr = _find_attr(key)
    if attr is None:
        return default
    try:
        val = attr.GetValue(weapon)
        return float(val[0]) if isinstance(val, (tuple, list)) else float(val)
    except Exception:
        return default


# --- Crit Calculation ---

_CRIT_ATTR_PATH = "D_Attributes.GameplayAttributes.PlayerCriticalHitBonus"

# All weapon part slot names on DefinitionData.
_PART_SLOTS = (
    "WeaponTypeDefinition",
    "BalanceDefinition",
    "ManufacturerDefinition",
    "BodyPartDefinition",
    "GripPartDefinition",
    "BarrelPartDefinition",
    "SightPartDefinition",
    "StockPartDefinition",
    "ElementalPartDefinition",
    "Accessory1PartDefinition",
    "Accessory2PartDefinition",
    "MaterialPartDefinition",
)


def _get_weapon_crit_parts(weapon: UObject) -> tuple[float, float, float, float]:
    """Collect crit modifiers from all weapon parts' ExternalAttributeEffects.

    Uses the bl2.parts attribute formula which separates bonuses by modifier
    type and by sign for Scale modifiers:
        Final = (Base + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd

    Returns (crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd).
    crit_neg_scale will be <= 0 (already negative).
    """
    crit_preadd = 0.0
    crit_pos_scale = 0.0
    crit_neg_scale = 0.0
    crit_postadd = 0.0
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd

    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
        except Exception:
            continue
        if part is None:
            continue
        try:
            effects = part.ExternalAttributeEffects
        except Exception:
            continue
        if effects is None:
            continue
        for effect in effects:
            try:
                attr = effect.AttributeToModify
                if attr is None:
                    continue
                if attr.PathName(attr) != _CRIT_ATTR_PATH:
                    continue
                modifier = effect.BaseModifierValue
                bonus_value = modifier.BaseValueConstant * modifier.BaseValueScaleConstant
                modifier_type = str(effect.ModifierType)
                if "PreAdd" in modifier_type:
                    crit_preadd += bonus_value
                elif "PostAdd" in modifier_type:
                    crit_postadd += bonus_value
                else:
                    # Scale (default) - positive and negative values go in
                    # different buckets per the bl2.parts formula.
                    if bonus_value >= 0:
                        crit_pos_scale += bonus_value
                    else:
                        crit_neg_scale += bonus_value  # already negative
            except Exception:
                continue
    return crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd


# --- BPD Scanning ---
#
# BehaviorProviderDefinitions (BPDs) contain behavior trees that can change
# how a weapon fires, consumes ammo, or deals damage. We scan them for two
# purposes:
#   1. Detect unknown DPS-affecting behaviors -> show (!) warning
#   2. Read splash damage scale from Behavior_Explode instances

# Behaviors in this set are cosmetic / visual and don't affect DPS.
_HARMLESS_BEHAVIORS: set[str] = {
    "Behavior_RunBehaviorCollection",
    "Behavior_CompareFloat",
    "Behavior_SetFloatParam",
    "Behavior_SetVectorParam",
    "Behavior_SetObjectParam",
    "Behavior_AddInstanceData",
    "Behavior_ChangeSpin",
    "Behavior_Explode",  # splash damage - read separately by _get_bpd_splash_scale
    "ProjectileBehavior_Detonate",
}


def _get_bpd_behavior_names(bpd: UObject) -> list[str]:
    """Return class names of all behaviors inside a BPD.

    Used by both the DPS-behavior detector and the splash scale reader.
    """
    names: list[str] = []
    try:
        sequences = bpd.BehaviorSequences
        if sequences is None:
            return names
        for seq in sequences:
            try:
                actions = seq.BehaviorData2
                if actions is None:
                    continue
                for action in actions:
                    try:
                        behavior = action.Behavior
                        if behavior is not None:
                            names.append(str(behavior.Class.Name))
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return names


def _get_bpd_splash_scale(bpd: UObject) -> float:
    """Read splash damage scale from Behavior_Explode instances in a BPD.

    Each Behavior_Explode has a DamageFormula with BaseValueScaleConstant
    representing the fraction of card damage dealt as additional splash
    (e.g. 0.8 = 80%, 1.0 = 100%).

    Returns the highest scale found, or 0.0 if no Behavior_Explode exists.
    """
    highest_scale = 0.0
    try:
        sequences = bpd.BehaviorSequences
        if sequences is None:
            return highest_scale
        for seq in sequences:
            try:
                actions = seq.BehaviorData2
                if actions is None:
                    continue
                for action in actions:
                    try:
                        behavior = action.Behavior
                        if behavior is None:
                            continue
                        if str(behavior.Class.Name) != "Behavior_Explode":
                            continue
                        scale = float(behavior.DamageFormula.BaseValueScaleConstant)
                        if scale > highest_scale:
                            highest_scale = scale
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return highest_scale


def _collect_all_bpds(weapon: UObject) -> list[UObject]:
    """Gather every BPD reachable from a weapon (type, projectile, all parts).

    Used by both the splash scanner and the unknown-behavior detector to
    avoid duplicating the traversal logic.
    """
    bpds: list[UObject] = []
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return bpds

    # Weapon type and its projectile
    try:
        weapon_type = definition_data.WeaponTypeDefinition
        if weapon_type is not None:
            bpd = weapon_type.BehaviorProviderDefinition
            if bpd is not None:
                bpds.append(bpd)
            firing_mode = weapon_type.DefaultFiringModeDefinition
            if firing_mode is not None:
                proj_def = firing_mode.ProjectileDefinition
                if proj_def is not None:
                    proj_bpd = proj_def.BehaviorProviderDefinition
                    if proj_bpd is not None:
                        bpds.append(proj_bpd)
    except Exception:
        pass

    # All parts and their custom firing mode projectiles
    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
            if part is None:
                continue
            bpd = part.BehaviorProviderDefinition
            if bpd is not None:
                bpds.append(bpd)
            try:
                cfm = part.CustomFiringModeDefinition
                if cfm is not None:
                    cproj = cfm.ProjectileDefinition
                    if cproj is not None:
                        cpbpd = cproj.BehaviorProviderDefinition
                        if cpbpd is not None:
                            bpds.append(cpbpd)
            except Exception:
                pass
        except Exception:
            continue

    return bpds


def _explode_scale_from_behaviors(behaviors) -> float:
    """Sum the splash scale of every Behavior_Explode in a behavior list.

    Only formulas based on the weapon's own damage attribute count, since
    their BaseValueScaleConstant is then a fraction of card damage
    (e.g. 0.9 = 90%). Anything else is not a card-relative value.
    """
    total = 0.0
    try:
        for behavior in behaviors:
            try:
                if behavior is None or str(behavior.Class.Name) != "Behavior_Explode":
                    continue
                formula = behavior.DamageFormula
                attr = formula.BaseValueAttribute
                if attr is None:
                    continue
                if not str(attr.PathName(attr)).endswith("Weapon.WeaponDamage"):
                    continue
                total += float(formula.BaseValueScaleConstant)
            except Exception:
                continue
    except Exception:
        pass
    return total


def _get_firing_mode_splash_scale(weapon: UObject) -> float:
    """Read splash from the weapon's active firing mode (OnAnyImpact).

    Bullet-type weapons with splash (e.g. Torgue assault rifles) keep their
    Behavior_Explode in FiringModeDefinition.OnAnyImpact rather than in a
    BPD. A part's CustomFiringModeDefinition replaces the weapon type's
    default firing mode when present.
    """
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return 0.0

    modes = []
    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
            if part is None:
                continue
            custom_mode = part.CustomFiringModeDefinition
            if custom_mode is not None:
                modes.append(custom_mode)
        except Exception:
            continue
    if not modes:
        try:
            default_mode = definition_data.WeaponTypeDefinition.DefaultFiringModeDefinition
            if default_mode is not None:
                modes.append(default_mode)
        except Exception:
            pass

    best = 0.0
    for mode in modes:
        try:
            best = max(best, _explode_scale_from_behaviors(mode.OnAnyImpact))
        except Exception:
            continue
    return best


def _get_weapon_splash_scale(weapon: UObject) -> float:
    """Return the splash damage scale for a weapon.

    Takes the larger of the BPD-based scale (projectile explosions, e.g.
    launchers) and the firing-mode scale (bullet splash, e.g. Torgue ARs).
    The larger is used rather than the sum so a single explosion that is
    visible through both routes is not counted twice.
    """
    highest_scale = _get_firing_mode_splash_scale(weapon)
    for bpd in _collect_all_bpds(weapon):
        scale = _get_bpd_splash_scale(bpd)
        if scale > highest_scale:
            highest_scale = scale
    return highest_scale


def _log(msg: str) -> None:
    try:
        unrealsdk.logging.info(msg)
    except Exception:
        print(msg)


def _path(obj: UObject | None) -> str:
    if obj is None:
        return "None"
    try:
        return str(obj.PathName(obj))
    except Exception:
        return "<unreadable>"


def _probe(label: str, getter) -> UObject | None:
    """Run getter(); log the result path or the error. Returns the value."""
    try:
        value = getter()
        _log(f"[DPS debug]   {label}: {_path(value)}")
        return value
    except Exception as e:
        _log(f"[DPS debug]   {label}: ERROR {type(e).__name__}: {e}")
        return None


_DEBUG_KEYWORDS = (
    "explo", "splash", "radius", "impact", "blast",
    "projectile", "behavior", "area", "damage",
    "custom", "onany",
)


_RECURSE_KEYWORDS = (
    "projectile", "explo", "impact", "behavior", "splash", "blast",
    "custom", "onany",
)
_debug_seen: set[str] = set()


def _dump_behaviors(label: str, obj) -> None:
    """If obj is (or has) a BPD, log its behaviors and any explode formulas."""
    bpd = None
    try:
        obj.BehaviorSequences
        bpd = obj
    except Exception:
        try:
            bpd = obj.BehaviorProviderDefinition
        except Exception:
            bpd = None
    if bpd is None:
        return
    _log(
        f"[DPS debug]   BPD via {label}: {_path(bpd)}"
        f" -> {_get_bpd_behavior_names(bpd)}"
    )
    try:
        for seq in bpd.BehaviorSequences:
            for action in seq.BehaviorData2:
                b = action.Behavior
                if b is None:
                    continue
                cname = str(b.Class.Name)
                if "explo" not in cname.lower():
                    continue
                try:
                    f = b.DamageFormula
                    attr, init = f.BaseValueAttribute, f.InitializationDefinition
                    _log(
                        f"[DPS debug]   {cname} const={f.BaseValueConstant}"
                        f" attr={_path(attr) if attr else None}"
                        f" init={_path(init) if init else None}"
                        f" scale={f.BaseValueScaleConstant}"
                    )
                except Exception as e:
                    _log(f"[DPS debug]   {cname} formula error {type(e).__name__}: {e}")
    except Exception as e:
        _log(f"[DPS debug]   behavior scan error {type(e).__name__}: {e}")


def _debug_follow(label: str, value, depth: int) -> None:
    """Dump objects referenced by a field (single objects or arrays of them)."""
    if value is None:
        return
    items = [value]
    try:
        if not isinstance(value, str) and hasattr(value, "__len__"):
            items = list(value)[:5]
    except Exception:
        items = [value]
    for i, item in enumerate(items):
        if item is None:
            continue
        try:
            path = str(item.PathName(item))
            if str(item.Class.Name) in ("ScriptStruct", "Class"):
                continue  # struct / class definitions are just noise
        except Exception:
            continue  # a struct or plain value, not an object
        if path in _debug_seen:
            continue
        sub = label if len(items) == 1 else f"{label}[{i}]"
        debug_dump_fields(sub, item, list_all=False, depth=depth)
        _dump_behaviors(sub, item)


def _short(value) -> str:
    """Short printable form of any value: object path, or truncated repr."""
    if value is None:
        return "None"
    try:
        return str(value.PathName(value))
    except Exception:
        pass
    try:
        text = repr(value)
    except Exception:
        text = "<unrepresentable>"
    return text if len(text) <= 300 else text[:300] + "..."


def debug_dump_fields(label: str, obj, list_all: bool = True, depth: int = 0) -> None:
    """Log an object's field names and the values of damage-related fields."""
    if obj is None:
        _log(f"[DPS debug] FIELDS {label}: None")
        return
    try:
        _debug_seen.add(_path(obj))
        names: list[str] = []
        cls = obj.Class
        for _ in range(20):
            if cls is None:
                break
            try:
                names.extend(str(f.Name) for f in cls._fields())
            except Exception as e:
                _log(f"[DPS debug]   _fields error {type(e).__name__}: {e}")
            try:
                cls = cls._superfield
            except Exception:
                break
        _log(
            f"[DPS debug] FIELDS {label} ({_path(obj)}), class"
            f" {obj.Class.Name}: {len(names)} fields"
        )
        if list_all:
            _log("[DPS debug]   names: " + ", ".join(names))
        for name in names:
            lowered = name.lower()
            if not any(k in lowered for k in _DEBUG_KEYWORDS):
                continue
            try:
                value = getattr(obj, name)
                text = _short(value)
                if text.startswith("<bound function"):
                    continue
                _log(f"[DPS debug]   {name} = {text}")
            except Exception as e:
                _log(f"[DPS debug]   {name} = ERROR {type(e).__name__}: {e}")
                continue
            if depth < 2 and any(k in lowered for k in _RECURSE_KEYWORDS):
                _debug_follow(f"{label}.{name}", value, depth + 1)
    except Exception as e:
        _log(f"[DPS debug] FIELDS {label} failed: {type(e).__name__}: {e}")


def debug_dump_weapon(weapon: UObject, stats: dict[str, float | bool]) -> None:
    """Log everything the splash scanner can see for this weapon."""
    try:
        _log("[DPS debug] ===== weapon =====")
        _debug_seen.clear()
        _log(
            "[DPS debug] stats: "
            + ", ".join(
                f"{k}={stats.get(k)}"
                for k in (
                    "damage", "projectiles", "fire_rate", "mag_size",
                    "reload_time", "shot_cost", "crit_mult",
                    "splash_scale", "pure_splash", "has_bpd",
                )
            )
        )

        # Where do BPDs come from?
        definition_data = _probe("DefinitionData", lambda: weapon.DefinitionData)
        wtype = _probe(
            "WeaponTypeDefinition",
            lambda: weapon.DefinitionData.WeaponTypeDefinition,
        )
        if wtype is not None:
            _probe("type.BehaviorProviderDefinition", lambda: wtype.BehaviorProviderDefinition)
            fmode = _probe("type.DefaultFiringModeDefinition", lambda: wtype.DefaultFiringModeDefinition)
            if fmode is not None:
                proj = _probe("firing_mode.ProjectileDefinition", lambda: fmode.ProjectileDefinition)
                if proj is not None:
                    _probe("projectile.BehaviorProviderDefinition", lambda: proj.BehaviorProviderDefinition)
        if definition_data is not None:
            for slot in _PART_SLOTS:
                try:
                    part = getattr(definition_data, slot)
                except Exception:
                    continue
                if part is None:
                    continue
                bpd_path = "ERROR"
                try:
                    bpd_path = _path(part.BehaviorProviderDefinition)
                except Exception as e:
                    bpd_path = f"ERROR {type(e).__name__}: {e}"
                _log(f"[DPS debug]   {slot}: {_path(part)} | BPD: {bpd_path}")

        # What is inside each BPD the scanner actually visits?
        bpds = _collect_all_bpds(weapon)
        _log(f"[DPS debug] scanner visits {len(bpds)} BPD(s)")
        for bpd in bpds:
            names = _get_bpd_behavior_names(bpd)
            _log(f"[DPS debug] BPD {_path(bpd)}: {names}")
            try:
                for seq in bpd.BehaviorSequences:
                    for action in seq.BehaviorData2:
                        b = action.Behavior
                        if b is None or str(b.Class.Name) != "Behavior_Explode":
                            continue
                        f = b.DamageFormula
                        attr = f.BaseValueAttribute
                        init = f.InitializationDefinition
                        _log(
                            f"[DPS debug]   EXPLODE const={f.BaseValueConstant}"
                            f" attr={_path(attr) if attr else None}"
                            f" init={_path(init) if init else None}"
                            f" scale={f.BaseValueScaleConstant}"
                        )
            except Exception as e:
                _log(f"[DPS debug]   explode scan error {type(e).__name__}: {e}")

        _log(
            "[DPS debug] firing-mode splash scale ="
            f" {_get_firing_mode_splash_scale(weapon)}, BPD splash scale ="
            f" {max([_get_bpd_splash_scale(b) for b in _collect_all_bpds(weapon)] or [0.0])}"
        )

        # Field dump: find where splash data actually lives.
        try:
            fm = wtype.DefaultFiringModeDefinition if wtype is not None else None
        except Exception:
            fm = None
        debug_dump_fields("default firing mode", fm)
        debug_dump_fields("weapon type", wtype)
        for slot in (
            "BarrelPartDefinition",
            "BodyPartDefinition",
            "ElementalPartDefinition",
            "BalanceDefinition",
        ):
            try:
                part = getattr(definition_data, slot)
            except Exception:
                part = None
            debug_dump_fields(slot, part, list_all=(slot == "BarrelPartDefinition"))
        debug_dump_fields("weapon instance", weapon, list_all=False)
        _log("[DPS debug] ===== end weapon =====")
    except Exception as e:
        _log(f"[DPS debug] dump failed: {type(e).__name__}: {e}")


def _has_nonstandard_bpd(weapon: UObject) -> bool:
    """Return True if any weapon BPD contains unknown DPS-affecting behaviors.

    Also returns True if any part has a CustomFiringModeDefinition, since
    normal weapons never have one and it indicates special projectile
    behaviour we can't calculate without building a tool to evaluate
    without building a proper interpreter for BPDs.
    """
    for bpd in _collect_all_bpds(weapon):
        for name in _get_bpd_behavior_names(bpd):
            if name not in _HARMLESS_BEHAVIORS:
                return True

    # Custom firing modes on parts indicate special projectile behaviour
    try:
        definition_data = weapon.DefinitionData
        for slot in _PART_SLOTS:
            try:
                part = getattr(definition_data, slot)
                if part is not None and part.CustomFiringModeDefinition is not None:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


# --- Pure Splash Detection ---


def _is_pure_splash(weapon: UObject) -> bool:
    """Return True if the weapon delivers ALL its card damage as splash.

    Rocket-type weapons (FireType == EWWFT_Rocket) deal all damage through
    HurtRadius which cannot crit. Bullet/hitscan weapons deliver card
    damage as a direct hit (crittable) even if they also have splash on top.
    """
    try:
        weapon_type = weapon.DefinitionData.WeaponTypeDefinition
        if weapon_type is None:
            return False
        # EWWFT_Rocket == 2
        firing_mode = weapon_type.DefaultFiringModeDefinition
        if firing_mode is not None:
            fire_type = str(firing_mode.FireType)
            if "Rocket" in fire_type or fire_type == "2":
                return True
        # Fallback: weapon type name
        type_name = str(weapon_type.Name) if weapon_type.Name is not None else ""
        if "Launcher" in type_name:
            return True
    except Exception:
        pass
    return False


# --- Vanilla BPD Assumptions ---
#
# These corrections assume unmodded weapon archetypes. Only applied when
# the "Assume Vanilla BPDs" option is enabled, since mods may change what
# these weapons do.

_VANILLA_MAG_SIZE_OVERRIDES: dict[tuple[str, str], float] = {
    ("Vladof", "Launcher"): 1.5,  # every 3rd shot is free
}

_VANILLA_SPLASH_BARRELS: tuple[str, ...] = (
    "AR_Barrel_Torgue",  # converts bullets to grenades (pure splash)
)


def _get_manufacturer_name(weapon: UObject) -> str:
    try:
        mfr = weapon.DefinitionData.ManufacturerDefinition
        if mfr is not None:
            return str(mfr.Name)
    except Exception:
        pass
    return ""


def _get_weapon_type_name(weapon: UObject) -> str:
    try:
        wtype = weapon.DefinitionData.WeaponTypeDefinition
        if wtype is not None:
            return str(wtype.Name)
    except Exception:
        pass
    return ""


def _is_vanilla_splash_barrel(weapon: UObject) -> bool:
    """Return True if the barrel is known to convert bullets to pure splash."""
    try:
        barrel = weapon.DefinitionData.BarrelPartDefinition
        if barrel is not None:
            barrel_path = str(barrel.PathName(barrel))
            for pattern in _VANILLA_SPLASH_BARRELS:
                if pattern in barrel_path:
                    return True
    except Exception:
        pass
    return False


def _get_vanilla_mag_size_mult(weapon: UObject) -> float | None:
    """Return the mag size multiplier for a known vanilla BPD combo, or None."""
    manufacturer = _get_manufacturer_name(weapon)
    type_name = _get_weapon_type_name(weapon)
    for (mfr_pattern, type_pattern), multiplier in _VANILLA_MAG_SIZE_OVERRIDES.items():
        if mfr_pattern in manufacturer and type_pattern in type_name:
            return multiplier
    return None


# --- Jakobs Semi-Automatic Detection ---

# Jakobs pistols, assault rifles and shotguns fire once per trigger pull.
# Matched against the weapon type's path (e.g. GD_Weap_AssaultRifle.A_Weapons...).
_JAKOBS_SEMI_AUTO_TYPES: tuple[str, ...] = ("Pistol", "AssaultRifle", "Shotgun")


def _is_jakobs_semi_auto(weapon: UObject) -> bool:
    """Return True for Jakobs weapons that fire once per trigger pull."""
    if "Jakobs" not in _get_manufacturer_name(weapon):
        return False
    try:
        weapon_type = weapon.DefinitionData.WeaponTypeDefinition
        path = str(weapon_type.PathName(weapon_type))
    except Exception:
        return False
    return any(kind in path for kind in _JAKOBS_SEMI_AUTO_TYPES)


# --- Element Detection ---


def _has_flesh_armor_element(weapon: UObject) -> bool:
    """Return True if the weapon is incendiary or corrosive.

    Checks each part's CustomDamageTypeDefinition path for the element name
    (e.g. GD_Incendiary.DamageType.DmgType_Incendiary_Impact). Incendiary is
    strong against flesh and corrosive against armor; other elements have no
    optimal-target bonus.
    """
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return False
    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
            if part is None:
                continue
            damage_type = part.CustomDamageTypeDefinition
            if damage_type is None:
                continue
            path = str(damage_type.PathName(damage_type))
            if "Incendiary" in path or "Corrosive" in path:
                return True
        except Exception:
            continue
    return False


# --- Stat Collection ---


def get_weapon_stats(weapon: UObject) -> dict[str, float | bool]:
    """Read all weapon stats into a dict.

    Every key from WEAPON_ATTRIBUTES is included with its resolved float
    value, plus the following derived entries:

    Derived floats:
        fire_rate      - Shots per second (1 / fire_interval).
        crit_mult      - Crit multiplier from weapon parts using the
                         bl2.parts attribute formula.
        splash_scale   - Fraction of card damage dealt as additional splash
                         (e.g. 0.8 = 80%).  Read from Behavior_Explode
                         instances in the weapon's BPDs.  0.0 if none.

    Flags (bool):
        has_bpd        - True if any BPD on the weapon contains behaviors
                         not in the known-harmless set, or if any part has
                         a CustomFiringModeDefinition.  The display appends
                         "(!)" to warn the user.
        pure_splash    - True if ALL card damage is delivered as splash
                         (e.g. rocket launchers).  Splash cannot crit.
    """
    stats: dict[str, float | bool] = {
        key: read_weapon_stat(weapon, key) for key in WEAPON_ATTRIBUTES
    }

    # --- Derived floats ---

    # Weapons with shot_cost 0 never consume ammo and never reload.
    # Set reload_time to 0 and shot_cost to 1 so the DPS formula
    # naturally simplifies to: damage * projectiles * fire_rate.
    if stats["shot_cost"] <= 0:
        stats["shot_cost"] = 1.0
        stats["reload_time"] = 0.0

    fire_interval = stats["fire_interval"]
    stats["fire_rate"] = (1.0 / fire_interval) if fire_interval > 0 else 0.0

    # Jakobs semi-auto weapons: the listed fire rate is a ceiling, but the
    # player's trigger finger is usually the limit.
    stats["fire_rate_cap"] = 0.0
    if limit_jakobs_fire_rate.value and _is_jakobs_semi_auto(weapon):
        click_rate = float(jakobs_clicks_per_second.value)
        if 0 < click_rate < stats["fire_rate"]:
            stats["fire_rate"] = click_rate
            stats["fire_rate_cap"] = click_rate

    # crit_neg_scale is already negative, so (1 - negative) == (1 + abs).
    crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd = _get_weapon_crit_parts(weapon)
    neg_scale_divisor = 1.0 - crit_neg_scale
    if neg_scale_divisor <= 0:
        neg_scale_divisor = 0.001
    stats["crit_mult"] = (2.0 + crit_preadd) * (
        1.0 + crit_pos_scale
    ) / neg_scale_divisor + crit_postadd

    stats["splash_scale"] = _get_weapon_splash_scale(weapon)

    # Optimal-target elemental bonus (incendiary vs flesh, corrosive vs armor).
    element_mult = 1.0
    if assume_optimal_element.value and _has_flesh_armor_element(weapon):
        element_mult = 1.5 if normal_mode_element_bonus.value else 1.75
    stats["element_mult"] = element_mult

    # --- Vanilla BPD corrections ---

    vanilla_corrected = False
    if assume_vanilla_bpds.value:
        vanilla_mag_mult = _get_vanilla_mag_size_mult(weapon)
        if vanilla_mag_mult is not None:
            stats["mag_size"] = float(stats["mag_size"]) * vanilla_mag_mult
            vanilla_corrected = True

    # --- Flags ---

    has_bpd = _has_nonstandard_bpd(weapon)
    if vanilla_corrected:
        has_bpd = False
    stats["has_bpd"] = has_bpd

    pure_splash = _is_pure_splash(weapon)
    if assume_vanilla_bpds.value and _is_vanilla_splash_barrel(weapon):
        pure_splash = True
    stats["pure_splash"] = pure_splash

    return stats


# --- DPS Calculation ---
#
# To use a custom formula, replace calc_dps() with your own function that
# takes a stats dict (from get_weapon_stats) and returns a tuple or None.


def calc_dps(stats: dict[str, float | bool]) -> tuple[float, float] | None:
    """Calculate weapon DPS, split into impact and splash components.

    Impact DPS is crittable. Splash DPS is not.

    Formula:
        single_shot_damage = damage * projectiles
        shots_per_mag      = mag_size / shot_cost
        base_dps = (single_shot_damage * fire_rate * shots_per_mag)
                 / (fire_rate * reload_time + shots_per_mag)

    Normal weapons (direct hit + splash on top):
        impact_dps = base_dps
        splash_dps = base_dps * splash_scale

    Pure-splash weapons (launchers, Torgue barrels): the card damage IS the
    explosion, so there is no separate impact hit and nothing is added on
    top:
        impact_dps = 0
        splash_dps = base_dps * (splash_scale, or 1.0 if none was found)

    Returns:
        (impact_dps, splash_dps) tuple, or None if stats are invalid.
    """
    fire_rate = stats["fire_rate"]
    mag_size = stats["mag_size"]
    shot_cost = stats["shot_cost"]
    reload_time = stats["reload_time"]
    damage = stats["damage"]
    projectile_count = stats["projectiles"]
    splash_scale = float(stats.get("splash_scale", 0.0))

    if fire_rate <= 0 or mag_size <= 0:
        return None

    shots_per_mag = mag_size / shot_cost
    single_shot_damage = damage * max(projectile_count, 1.0)

    time_per_mag_cycle = fire_rate * reload_time + shots_per_mag
    if time_per_mag_cycle <= 0:
        return None

    base_dps = (single_shot_damage * fire_rate * shots_per_mag) / time_per_mag_cycle

    if bool(stats.get("pure_splash", False)):
        scale = splash_scale if splash_scale > 0 else 1.0
        return 0.0, base_dps * scale

    return base_dps, base_dps * splash_scale


# --- Display ---


class DpsReadout(NamedTuple):
    """Everything needed to display a weapon's DPS."""

    dps: float
    crit_dps: float | None
    has_nonstandard_bpd: bool = False
    element_mult: float = 1.0
    fire_rate_cap: float = 0.0


def compute_readout(weapon: UObject, dump_debug: bool = True) -> DpsReadout | None:
    """Work out the DPS readout for a weapon, or None if it can't be done."""
    stats = get_weapon_stats(weapon)
    if debug_logging.value and dump_debug:
        debug_dump_weapon(weapon, stats)

    result = calc_dps(stats)
    if result is None:
        return None

    impact_dps, splash_dps = result
    element_mult = float(stats.get("element_mult", 1.0))
    impact_dps *= element_mult
    splash_dps *= element_mult
    total_dps = impact_dps + splash_dps
    crit_mult = float(stats["crit_mult"])

    if bool(stats["pure_splash"]):
        crit_dps = None
    elif crit_mult != 1.0:
        # Only impact damage can crit, splash cannot
        crit_dps = impact_dps * crit_mult + splash_dps
    else:
        crit_dps = None

    return DpsReadout(
        dps=total_dps,
        crit_dps=crit_dps,
        has_nonstandard_bpd=bool(stats["has_bpd"]),
        element_mult=element_mult,
        fire_rate_cap=float(stats.get("fire_rate_cap", 0.0)),
    )


def _diff_text(diff: float) -> str:
    """Coloured +/- difference for the item card, or nothing if negligible."""
    if abs(diff) < 0.5:
        return ""
    color = "#33ff33" if diff > 0 else "#ff3333"
    sign = "+" if diff > 0 else "-"
    return f" <font color='{color}'>({sign}{abs(diff):,.0f})</font>"


def _dps_lines(readout: DpsReadout, compare: DpsReadout | None = None) -> list[str]:
    """The DPS lines shared by the popup and the item card.

    Pass `compare` (card only) to append the colour-coded difference against
    another weapon's readout.
    """
    warning = " (!)" if readout.has_nonstandard_bpd else ""
    element_note = (
        f" (x{readout.element_mult:g} element)" if readout.element_mult != 1.0 else ""
    )
    cap_note = (
        f" ({readout.fire_rate_cap:g} clicks/s)" if readout.fire_rate_cap > 0 else ""
    )
    dps_diff = _diff_text(readout.dps - compare.dps) if compare is not None else ""
    lines = [f"DPS: {readout.dps:,.0f}{dps_diff}{element_note}{cap_note}{warning}"]
    if readout.crit_dps is not None:
        crit_diff = ""
        if compare is not None and compare.crit_dps is not None:
            crit_diff = _diff_text(readout.crit_dps - compare.crit_dps)
        lines.append(f"Crit DPS: {readout.crit_dps:,.0f}{crit_diff}{warning}")
    return lines


def _format_card_text(readout: DpsReadout, compare: DpsReadout | None = None) -> str:
    """DPS as bullet lines in the same style as the card's own bonus text."""
    return "".join(
        f"\u2022 <font color='#ffffff'>{line}</font>\n"
        for line in _dps_lines(readout, compare)
    )


def _show_popup(readout: DpsReadout) -> None:
    pc = get_pc()
    if pc is None:
        return
    text = "\n".join(_dps_lines(readout))
    try:
        movie = pc.myHUD.HUDMovie
        if movie is not None:
            movie.AddTrainingText(
                text,
                "Weapon Stats",
                display_duration.value,
                unrealsdk.make_struct("Color"),
                "",
                False,
                0,
                pc.PlayerReplicationInfo,
                True,
                0,
                0,
            )
    except Exception:
        pass


# --- Hooks ---
#
# The item card builds itself inside SetItemCardEx by calling SetCardUIStats,
# SetTopStat (the five stat rows), SetTitle, SetFunStats (the bullet text
# under the stats), SetValue, SetLevelRequirement and SetHeight, in that
# order. So:
#   1. PRE SetItemCardEx:  work out the DPS for the weapon being shown.
#   2. PRE SetFunStats:    put the DPS lines at the top of the bullet text.
#   3. POST SetItemCardEx: show the popup, and as a fallback add the lines
#                          ourselves if SetFunStats was never called.

_current_readout: DpsReadout | None = None
_pending_card_text: str | None = None
_original_fun_stats: str | None = None
_in_card_update = False


def _card_log(msg: str) -> None:
    if debug_logging.value:
        _log(f"[DPS card] {msg}")


@hook("WillowGame.ItemCardGFxObject:SetItemCardEx", Type.PRE)
def _on_set_item_card_pre(
    _obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    global _current_readout, _pending_card_text, _original_fun_stats
    _current_readout = None
    _pending_card_text = None
    _original_fun_stats = None

    try:
        item = args.InventoryItem
        if item is None or item.Class.Name != "WillowWeapon":
            return
        readout = compute_readout(item)
    except Exception as e:
        _card_log(f"PRE SetItemCardEx failed: {type(e).__name__}: {e}")
        return

    compare_readout: DpsReadout | None = None
    if readout is not None and show_dps_comparison.value:
        try:
            compare_item = args.CompareAgainstInventoryItem
            if compare_item is not None and compare_item.Class.Name == "WillowWeapon":
                compare_readout = compute_readout(compare_item, dump_debug=False)
        except Exception as e:
            _card_log(f"comparison failed: {type(e).__name__}: {e}")

    _current_readout = readout
    if readout is not None and show_dps_on_card.value:
        _pending_card_text = _format_card_text(readout, compare_readout)
    _card_log(
        f"PRE SetItemCardEx: readout={readout is not None},"
        f" compared={compare_readout is not None},"
        f" card text queued={_pending_card_text is not None}"
    )


@hook("WillowGame.ItemCardGFxObject:SetFunStats", Type.PRE)
def _on_set_fun_stats(
    _obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    global _original_fun_stats
    if _in_card_update or not _pending_card_text:
        return
    try:
        original = str(args.FunStatsText)
    except Exception as e:
        _card_log(f"SetFunStats: could not read text: {type(e).__name__}: {e}")
        return
    _original_fun_stats = original
    # First attempt: change the text as the card receives it.
    try:
        args.FunStatsText = _pending_card_text + original
        _card_log("SetFunStats: text replaced in the call arguments")
    except Exception as e:
        _card_log(f"SetFunStats: could not change text: {type(e).__name__}: {e}")


@hook("WillowGame.ItemCardGFxObject:SetItemCardEx", Type.POST)
def _on_set_item_card(
    _obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    global _current_readout, _pending_card_text, _original_fun_stats
    global _in_card_update
    readout = _current_readout
    card_text = _pending_card_text
    original = _original_fun_stats
    _current_readout = None
    _pending_card_text = None
    _original_fun_stats = None

    if readout is None:
        return

    # Second attempt: once the card has finished building, set the bullet
    # text again with our lines on top. This produces the same text as the
    # first attempt, so it is harmless if that already worked, and it fixes
    # the card if the game ignored the change to the arguments.
    if card_text:
        _in_card_update = True
        try:
            _obj.SetFunStats(card_text + (original or ""))
            _obj.SetHeight()
            _card_log("POST SetItemCardEx: card text set again and height updated")
        except Exception as e:
            _card_log(f"POST SetItemCardEx: update failed: {type(e).__name__}: {e}")
        finally:
            _in_card_update = False

    if show_dps_popup.value:
        _show_popup(readout)


# --- Build Mod ---

mod = build_mod(
    options=[
        display_duration,
        assume_vanilla_bpds,
        show_dps_on_card,
        show_dps_comparison,
        show_dps_popup,
        assume_optimal_element,
        normal_mode_element_bonus,
        limit_jakobs_fire_rate,
        jakobs_clicks_per_second,
        debug_logging,
    ],
)
