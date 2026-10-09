# -*- coding: utf-8 -*-
"""capture refuses to start unless the data root is safe (SPEC 10.1): outside every git work tree, an ACL whose
entries all inherit (OI)(CI), and excluded from Windows Search while the indexer runs.

The ACL and Search-rule readers are the ones the old repo's tools/baseline_check.py used (acl_info,
_search_rules), carried here so the pipeline has no runtime dependency on that checkout.

Paths are taken as str or os.PathLike and handed to git, PowerShell and the comparison with the Search rules as
their text (os.fspath). The ACL and Search checks are Windows-only: off Windows check() refuses."""
import json
import os
import re
import subprocess

from .config import ConfigError


def in_work_tree(data_root):
    """True unless git positively says data_root is outside every work tree. safe.directory=*: as SYSTEM git
    refuses an admin-owned repo (dubious ownership, rc 128), which must not read as 'not a repository'."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-c", "safe.directory=*", "-C", os.fspath(data_root),
                            "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, LC_ALL="C", LANGUAGE="C"))
    except (OSError, subprocess.SubprocessError):
        return True
    if r.returncode == 0:
        return r.stdout.strip() != "false"
    return "not a git repository" not in r.stderr


def acl_info(paths):
    """{path: {path, protected, owner, rules: [{sid, rights, inh, prop, inherited, type}]}} via Get-Acl, keyed by
    each path's text (os.fspath)."""
    lst = ",".join("'" + os.fspath(p).replace("'", "''") + "'" for p in paths)
    script = (
        "$r=@(); foreach($p in @(" + lst + ")){ if(-not (Test-Path -LiteralPath $p)){ $r+=@{path=$p;missing=$true}; continue };"
        " $a=Get-Acl -LiteralPath $p; $rules=@(); foreach($x in $a.Access){ $sid=$x.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;"
        " $rules+=@{sid=$sid;rights=[string]$x.FileSystemRights;inh=[string]$x.InheritanceFlags;prop=[string]$x.PropagationFlags;inherited=$x.IsInherited;type=[string]$x.AccessControlType} };"
        " $o=$null; try{$o=(New-Object Security.Principal.NTAccount($a.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value}catch{};"
        " $r+=@{path=$p;protected=$a.AreAccessRulesProtected;owner=$o;rules=$rules} }; ConvertTo-Json -Depth 5 -Compress @($r)")
    # A PSModulePath inherited from pwsh 7 stops Windows PowerShell 5.1 from loading
    # Microsoft.PowerShell.Security (Get-Acl fails silently into an empty result).
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       env=env)
    if r.returncode != 0:
        raise RuntimeError(f"powershell rc={r.returncode}")
    out = r.stdout.decode("utf-8", "replace")
    if not out.strip():
        # baseline_check.ps_json returned None here and iterating it raised TypeError: capture exited 1
        # ("error TypeError"), not 2. Kept, so Task Scheduler's last-run result is the same as llm-wiki's.
        raise TypeError("Get-Acl printed nothing")
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    return {d["path"]: d for d in data}


def search_rules():
    """[(rule set, lower-case path, include 0/1)] of the Windows Search crawl scope ([] off Windows, as when the
    keys cannot be opened)."""
    if os.name != "nt":
        return []
    import winreg  # noqa: PLC0415
    base = r"SOFTWARE\Microsoft\Windows Search\CrawlScopeManager\Windows\SystemIndex"
    rules = []
    for sub in ("WorkingSetRules", "DefaultRules"):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + "\\" + sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                kk = winreg.OpenKey(k, name)
                url = winreg.QueryValueEx(kk, "URL")[0]
                inc = winreg.QueryValueEx(kk, "Include")[0]
            except OSError:
                continue
            m = re.match(r"^file:///([A-Za-z]):\\(?:\[[^\]]+\]\\)?(.*)$", url)
            if not m:
                continue
            path = (m.group(1).upper() + ":\\" + m.group(2)).rstrip("*").rstrip("\\").lower()
            rules.append((sub, path, int(inc)))
    return rules


def check(data_root):
    # the data parent allowlist is enforced by Config, before any command writes
    if in_work_tree(data_root):
        raise ConfigError("data root inside a git work tree (or git could not tell)")
    if os.name != "nt":
        raise ConfigError("data root ACL and Windows Search checks need Windows")
    root = os.fspath(data_root)     # acl_info keys its answer by the path's text
    info = acl_info([root]).get(root) or {}
    rules = info.get("rules") or []
    if not rules:
        raise ConfigError("data root ACL unreadable")
    for rule in rules:
        if "ContainerInherit" not in rule["inh"] or "ObjectInherit" not in rule["inh"]:
            raise ConfigError("data root ACL entry without (OI)(CI)")
    svc = subprocess.run(["sc", "query", "WSearch"], capture_output=True, text=True).stdout
    if "RUNNING" in svc:
        t = os.path.abspath(root).lower().rstrip("\\")    # text, compared with the rules' text
        best = None
        for _sub, p, inc in search_rules():
            if t == p or t.startswith(p + "\\"):
                if best is None or len(p) > len(best[0]):
                    best = (p, inc)
        if best is None or best[1] != 0:
            raise ConfigError("data root not excluded from Windows Search")
