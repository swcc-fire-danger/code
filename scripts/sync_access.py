"""Keep the ArcGIS groups in step with the Access Control page.

Leaders who are not ArcGIS group managers can give access on the website, but ArcGIS only lets
group owners and managers change group membership. This script finishes those changes with the
owner's ArcGIS sign-in (the AGOL_CLIENT_ID / AGOL_REFRESH_TOKEN GitHub secrets).

It reads the access table, applies the same rules as the website (a change only counts if the
person who saved it was allowed to make it), and then:
  - adds people to the Users / Guests / Leaders / Admins groups they should be in,
  - makes them group managers when their access says "Manager",
  - removes people only from groups they lost through a "Removed" change.
People added to a group directly in ArcGIS are never removed by this script.
If the secrets are not set, it does nothing.
"""
import os
import sys
import requests

PORTAL = os.environ.get("ARCGIS_PORTAL_URL", "https://nifc.maps.arcgis.com").rstrip("/")
REST = PORTAL + "/sharing/rest"
LEADERS_ITEM = "08d11d0fbf9f4ac8b9b25857cf32cd62"    # FDRA_Leaders (the access table)
GROUPS = {
    "pilot": "62660ea7de8b463b8e04765b124f9e26",     # SWCC Fire Danger Users
    "guests": "b8323391342e41a9965a9e233cf5f07d",    # SWCC Fire Danger Guests
    "leaders": "95e3e6ab264e4380b80a85b7b74b55ac",   # SWCC Fire Danger Leaders
    "admins": "71fd5cd4d96441fab40fa11f38e2c3e5",    # SWCC Fire Danger Admins
}
LEVELS = ["Guest", "Viewer", "FDRA Leader", "Zone Leader", "All Zones", "Admin"]   # "Viewer" is shown as "User"
LEADER_LEVELS = {"FDRA Leader", "Zone Leader", "All Zones"}
VIEW_LEVELS = {"Guest", "Viewer"}
RULES_V2_AT = 1794772800000    # 2026-11-15 20:00 UTC: rows saved before this keep the rules they were saved under
TOKEN = None


def arc(url, params=None, post=False):
    data = {"f": "json", **(params or {})}
    if TOKEN:
        data["token"] = TOKEN
    r = requests.post(url, data=data, timeout=60) if post else requests.get(url, params=data, timeout=60)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(j["error"].get("message", j["error"]))
    return j


def norm(v):
    return str(v or "").strip().lower()


def parse_role(text):
    t = str(text or "").strip()
    removed = t.lower().startswith("removed")
    if removed:
        t = t[7:].lstrip(" ·:|-")
    manager = False
    for sep in ("·", "|", "+"):
        head, _, tail = t.rpartition(sep)
        if head and tail.strip().lower() == "manager":
            manager, t = True, head.strip()
            break
    base = next((lv for lv in LEVELS if lv.lower() == t.lower()), None)
    if base is None and t.lower() in ("gacc", "gacc leader", "all zones", "all zone"):
        base = "All Zones"
    return base, manager, removed


class Access:
    def __init__(self, root):
        self.root = norm(root)
        self.people = {}       # user -> {scope_key: grant}
        self.history = []

    def grants(self, u):
        return list(self.people.get(norm(u), {}).values())

    def is_root(self, u): return norm(u) == self.root
    def admin_mgr(self, u): return self.is_root(u) or any(g["base"] == "Admin" and g["manager"] for g in self.grants(u))
    def admin(self, u): return self.admin_mgr(u) or any(g["base"] == "Admin" for g in self.grants(u))
    def mgr(self, u): return self.is_root(u) or any(g["manager"] for g in self.grants(u))
    def leads_all(self, u): return self.admin(u) or any(g["base"] == "All Zones" for g in self.grants(u))

    def leads_zone(self, u, z):
        return self.leads_all(u) or any(g["base"] == "Zone Leader" and norm(g["zone"]) == norm(z) for g in self.grants(u))

    def leads_fdra(self, u, key):
        if not key:
            return False
        zone = str(key).split(" ")[0]
        return self.leads_zone(u, zone) or any(g["base"] == "FDRA Leader" and norm(g["fdra"]) == norm(key) for g in self.grants(u))

    # area a person leads; mgr=True counts only the leader access they manage
    def lgr(self, u, mgr): return [g for g in self.grants(u) if g["base"] in LEADER_LEVELS and (not mgr or g["manager"])]
    def in_all(self, u, mgr): return any(g["base"] == "All Zones" for g in self.lgr(u, mgr))
    def in_zone(self, u, z, mgr): return self.in_all(u, mgr) or any(g["base"] == "Zone Leader" and norm(g["zone"]) == norm(z) for g in self.lgr(u, mgr))

    def in_fdra(self, u, key, mgr):
        if not key:
            return False
        return self.in_zone(u, str(key).split(" ")[0], mgr) or any(g["base"] == "FDRA Leader" and norm(g["fdra"]) == norm(key) for g in self.lgr(u, mgr))

    def in_scope(self, u, g, mgr):
        if g["fdra"]:
            return self.in_fdra(u, g["fdra"], mgr)
        if g["zone"]:
            return self.in_zone(u, g["zone"], mgr)
        return self.in_all(u, mgr)

    def may_change(self, by, g, removing):
        """Same rules as the website.
        Admin manager: anyone. Admin member: Guests, Users, FDRA and Zone Leaders anywhere.
        Leader manager: Guests, Users and leaders up to their level, in the area they manage.
        Leader member: adds Guests and Users in their area; cannot edit or remove.
        Nobody but the owner changes the owner's access or their own."""
        if g["at"] and g["at"] < RULES_V2_AT:
            return self.may_change_v1(by, g, removing)
        if not by or not g["base"]:
            return False
        if self.is_root(g["user"]) and not self.is_root(by):
            return False
        if not self.is_root(by) and norm(g["user"]) == norm(by):
            return False
        if self.admin_mgr(by):
            return True
        if g["base"] in ("Admin", "All Zones"):
            return False
        view = g["base"] in VIEW_LEVELS
        if view and g["manager"]:
            return False
        if self.admin(by):           # admin member
            return view or g["base"] in ("FDRA Leader", "Zone Leader")
        if view:
            return self.in_scope(by, g, bool(removing))
        if g["base"] == "Zone Leader":
            return self.in_zone(by, g["zone"], True)
        if g["base"] == "FDRA Leader":
            return self.in_fdra(by, g["fdra"], True)
        return False

    def may_change_v1(self, by, g, removing):
        if not by or not g["base"]:
            return False
        if self.is_root(g["user"]) and not self.is_root(by):
            return False
        if self.admin_mgr(by):
            return True
        if g["manager"] and not removing and not self.mgr(by):
            return False
        if g["base"] == "Admin":
            return self.admin(by) and not g["manager"]   # admin members add admin members only
        if g["base"] == "All Zones":
            return False
        if self.admin(by):
            return True
        if g["base"] == "Zone Leader":
            return False
        if g["base"] == "FDRA Leader":
            return self.leads_fdra(by, g["fdra"])
        if g["fdra"]:
            return self.leads_fdra(by, g["fdra"])
        if g["zone"]:
            return self.leads_zone(by, g["zone"])
        return self.leads_all(by)

    @staticmethod
    def key(g):
        scoped_zone = norm(g["zone"]) if g["base"] in VIEW_LEVELS or g["base"] == "Zone Leader" else ""
        return "|".join([g["base"], scoped_zone, norm(g["fdra"])])

    def build(self, rows, creator, created):
        items = []
        for a in rows:
            low = {k.lower(): v for k, v in a.items()}
            base, manager, removed = parse_role(low.get("role"))
            user = str(low.get("username") or "").strip()
            if not user or not base or user.lower() == "test":
                continue
            fk = low.get("fdra_zone") or None
            items.append({"user": user, "base": base, "manager": manager, "removed": removed,
                          "zone": low.get("zone") or (fk.split(" ")[0] if fk else None), "fdra": fk,
                          "by": a.get(creator), "at": a.get(created) or 0})
        for g in sorted(items, key=lambda x: x["at"]):
            g["ok"] = self.may_change(g["by"], g, g["removed"])
            if g["ok"]:
                p = self.people.setdefault(norm(g["user"]), {})
                if g["removed"]:
                    p.pop(self.key(g), None)
                else:
                    p[self.key(g)] = g
                if not p:
                    self.people.pop(norm(g["user"]), None)
            self.history.append(g)

    def want(self, u):
        """Guest: Guests member. User: Users member. Any leader: Users + Guests manager, Leaders member
        (manager for leader managers). Admin member: also Leaders manager + Admins member. Admin manager: manager everywhere."""
        gs, w = self.grants(u), {}
        if not gs:
            return w
        lead = [g for g in gs if g["base"] in LEADER_LEVELS]
        adm = [g for g in gs if g["base"] == "Admin"]
        if lead or adm:
            w["pilot"] = w["guests"] = "admin"
            w["leaders"] = "admin" if any(g["manager"] for g in lead) or adm else "member"
            if adm:
                w["admins"] = "admin" if any(g["manager"] for g in adm) else "member"
        elif any(g["base"] == "Viewer" for g in gs):
            w["pilot"] = "member"
        elif any(g["base"] == "Guest" for g in gs):
            w["guests"] = "member"
        return w

    def lost(self, u):
        out, w = set(), self.want(u)
        had = [h for h in self.history if h["ok"] and h["removed"] and norm(h["user"]) == norm(u)]
        for h in had:
            if h["base"] == "Guest":
                out.add("guests")
            if h["base"] == "Viewer":
                out.add("pilot")
            if h["base"] in LEADER_LEVELS:
                out |= {"leaders", "guests", "pilot"}
            if h["base"] == "Admin":
                out |= {"admins", "leaders", "guests", "pilot"}
        if had and not self.grants(u):
            out |= set(GROUPS)
        return out - set(w)


def members(group_id):
    out, start = {}, 1
    for _ in range(100):
        j = arc(f"{REST}/community/groups/{group_id}/userList", {"start": start, "num": 100})
        for u in j.get("users", []):
            out[norm(u["username"])] = (u["username"], u.get("memberType"))
        start = j.get("nextStart", -1)
        if not start or start < 0:
            break
    return out


def main():
    global TOKEN
    client_id = os.environ.get("AGOL_CLIENT_ID", "").strip()
    refresh = os.environ.get("AGOL_REFRESH_TOKEN", "").strip()
    if not client_id or not refresh:
        print("Access sync skipped: the AGOL_CLIENT_ID / AGOL_REFRESH_TOKEN secrets are not set.")
        return
    tok = requests.post(f"{REST}/oauth2/token", data={"client_id": client_id, "grant_type": "refresh_token",
                        "refresh_token": refresh, "expiration": 60, "f": "json"}, timeout=60).json()
    if "access_token" not in tok:
        print(f"Access sync skipped: ArcGIS sign-in failed ({tok.get('error', tok)}).")
        return
    TOKEN = tok["access_token"]

    item = arc(f"{REST}/content/items/{LEADERS_ITEM}")
    svc = arc(item["url"])
    layer_id = ((svc.get("tables") or svc.get("layers") or [{"id": 0}])[0])["id"]
    url = item["url"].rstrip("/") + f"/{layer_id}"
    info = arc(url)
    ef = info.get("editFieldsInfo") or {}
    rows, offset = [], 0
    while True:
        j = arc(url + "/query", {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                                 "resultOffset": offset, "resultRecordCount": 2000})
        feats = j.get("features", [])
        rows += [f["attributes"] for f in feats]
        offset += len(feats)
        if not j.get("exceededTransferLimit") or not feats:
            break

    acc = Access(item.get("owner"))
    acc.build(rows, ef.get("creatorField", "Creator"), ef.get("creationDateField", "CreationDate"))
    ignored = sum(1 for h in acc.history if not h["ok"])
    print(f"Access table: {len(rows)} changes, {len(acc.people)} people with access, {ignored} ignored (no permission)")

    current = {k: members(gid) for k, gid in GROUPS.items()}
    users = set(acc.people) | {norm(h["user"]) for h in acc.history if h["ok"]}
    done = failed = 0
    for u in sorted(users):
        if acc.is_root(u):
            continue    # the site owner's group membership is never changed
        want, lost = acc.want(u), acc.lost(u)
        name = next((g["user"] for g in acc.grants(u)), None) or next(h["user"] for h in acc.history if norm(h["user"]) == u)
        for k, gid in GROUPS.items():
            cur = current[k].get(u, (None, None))[1]
            w = want.get(k)
            if cur == "owner":
                continue
            try:
                if w and not cur:
                    j = arc(f"{REST}/community/groups/{gid}/addUsers", {"users": name}, post=True)
                    if j.get("notAdded"):
                        raise RuntimeError("not added")
                    if w == "admin":
                        arc(f"{REST}/community/groups/{gid}/updateUsers", {"admins": name}, post=True)
                    print(f"  added {name} to {k}{' as manager' if w == 'admin' else ''}")
                    done += 1
                elif w == "admin" and cur == "member":
                    arc(f"{REST}/community/groups/{gid}/updateUsers", {"admins": name}, post=True)
                    print(f"  made {name} a manager of {k}")
                    done += 1
                elif w == "member" and cur == "admin" and any(h["ok"] and h["removed"] and norm(h["user"]) == u for h in acc.history):
                    arc(f"{REST}/community/groups/{gid}/updateUsers", {"users": name}, post=True)
                    print(f"  {name} is no longer a manager of {k}")
                    done += 1
                elif not w and cur and k in lost:
                    if cur == "admin":   # a manager is made a member first, then removed
                        arc(f"{REST}/community/groups/{gid}/updateUsers", {"users": name}, post=True)
                    arc(f"{REST}/community/groups/{gid}/removeUsers", {"users": name}, post=True)
                    print(f"  removed {name} from {k}")
                    done += 1
            except Exception as e:  # keep going; the next run tries again
                print(f"  could not update {name} in {k}: {e}")
                failed += 1
    print(f"Access sync finished: {done} group changes, {failed} could not be applied")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the website build
        print(f"Access sync skipped: {e}")
    sys.exit(0)
