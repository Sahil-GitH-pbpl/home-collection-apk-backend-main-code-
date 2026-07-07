from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.repositories.panel_catalog_repository import PanelCatalogRepository


@dataclass
class TestMeta:
    gcode: str
    scode: str
    test_code: str
    testcode1: str
    description: str
    is_profile: bool


class PanelCatalogService:
    @staticmethod
    def _clean(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _to_bool_profile(value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text in {"1", "y", "yes", "true", "p", "profile"}

    @staticmethod
    def _to_number(value: Any) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else value
        text = str(value).strip()
        if text == "":
            return None
        try:
            parsed = float(text)
            return int(parsed) if parsed.is_integer() else parsed
        except ValueError:
            return None

    @staticmethod
    def _normalize_billing_mode(value: Any) -> str:
        seen: set[str] = set()
        out: list[str] = []
        for ch in str(value or "").upper():
            if ch in {"C", "P", "F"} and ch not in seen:
                seen.add(ch)
                out.append(ch)
        return "".join(out)

    def get_panel_companies(self, repository: PanelCatalogRepository) -> dict[str, Any]:
        panel_rows = repository.fetch_selected_panel_company_rows()
        panel_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in panel_rows:
            pname = self._clean(row.pname)
            comp = self._clean(row.CompCatID)
            if not pname or not comp:
                continue
            key = (pname.lower(), comp)
            item = panel_map.setdefault(
                key,
                {
                    "CenterID": row.CenterID,
                    "pname": pname,
                    "CompCatID": comp,
                    "CatDetails": self._clean(row.CatDetails),
                    "showmrp": 0,
                    "_modes": set(),
                },
            )
            mode = self._normalize_billing_mode(row.BillingChargeMode)
            for ch in mode:
                item["_modes"].add(ch)
            if int(row.showmrp or 0):
                item["showmrp"] = 1

        panels: list[dict[str, Any]] = []
        for panel in sorted(panel_map.values(), key=lambda x: (x["pname"].lower(), x["CompCatID"])):
            panel["BillingChargeMode"] = "".join(ch for ch in "CPF" if ch in panel["_modes"])
            panel.pop("_modes", None)
            panels.append(panel)
        return {"ok": True, "items": panels}

    def get_panel_catalog(self, repository: PanelCatalogRepository, comp_cat_id: str) -> dict[str, Any]:
        comp_id = self._clean(comp_cat_id)
        if not comp_id:
            raise ValueError("comp_cat_id is required")

        companies = self.get_panel_companies(repository)["items"]
        selected_company = next((x for x in companies if self._clean(x["CompCatID"]) == comp_id), None)

        group_rows = repository.fetch_group_rows()
        subgroup_rows = repository.fetch_subgroup_rows()
        test_rows = repository.fetch_test_rows()
        profile_rows = repository.fetch_test_profile_rows()
        panel_rate_rows = repository.fetch_panel_rate_rows_for_comp_id(comp_id)

        group_desc: dict[str, str] = {}
        for row in group_rows:
            g = self._clean(row.Gcode)
            if g and g not in group_desc:
                group_desc[g] = self._clean(row.Description)

        subgroup_desc: dict[tuple[str, str], str] = {}
        for row in subgroup_rows:
            g = self._clean(row.Gcode)
            s = self._clean(row.Scode)
            if g and s and (g, s) not in subgroup_desc:
                subgroup_desc[(g, s)] = self._clean(row.Description)

        test_by_g_s_testcode: dict[tuple[str, str, str], TestMeta] = {}
        test_by_testcode1: dict[str, TestMeta] = {}
        for row in test_rows:
            meta = TestMeta(
                gcode=self._clean(row.Gcode),
                scode=self._clean(row.Scode),
                test_code=self._clean(row.TestCode),
                testcode1=self._clean(row.Testcode1),
                description=self._clean(row.Description),
                is_profile=self._to_bool_profile(row.Profile),
            )
            if meta.gcode and meta.scode and meta.test_code:
                test_by_g_s_testcode[(meta.gcode, meta.scode, meta.test_code)] = meta
            if meta.testcode1 and meta.testcode1 not in test_by_testcode1:
                test_by_testcode1[meta.testcode1] = meta

        profile_children_map: dict[str, list[dict[str, Any]]] = {}
        profile_children_keys: set[tuple[str, str, str]] = set()
        for row in profile_rows:
            pg = self._clean(row.Gcode)
            ps = self._clean(row.SCode)
            ptest = self._clean(row.ProfileCode)
            child_tc1 = self._clean(row.TestCode)
            if not pg or not ps or not ptest or not child_tc1:
                continue
            child_meta = test_by_testcode1.get(child_tc1)
            if not child_meta:
                continue
            profile_children_keys.add((pg, ps, ptest))
            key = f"{pg}|{ps}|{ptest}"
            child = {
                "booked_code": child_meta.testcode1 or child_meta.test_code,
                "description": child_meta.description,
            }
            profile_children_map.setdefault(key, [])
            dkey = (child["booked_code"], child["description"])
            if dkey not in {
                (x["booked_code"], x["description"])
                for x in profile_children_map[key]
            }:
                profile_children_map[key].append(child)

        groups_map: dict[str, dict[str, Any]] = {}
        dedupe: set[tuple[str, str, str, str]] = set()
        for row in panel_rate_rows:
            g = self._clean(row.GCode)
            s = self._clean(row.SCode)
            tc = self._clean(row.TestCode)
            ctc = self._clean(row.CTestCode)

            meta = test_by_g_s_testcode.get((g, s, tc))
            if not meta and ctc:
                meta = test_by_testcode1.get(ctc)

            final_g = meta.gcode if meta else g
            final_s = meta.scode if meta else s
            final_tc = meta.test_code if meta else tc
            final_tc1 = meta.testcode1 if meta else ctc
            final_desc = meta.description if meta and meta.description else self._clean(row.CTestName)
            booked = final_tc1 or final_tc

            if not final_g or not final_s or not booked:
                continue
            dkey = (comp_id, final_g, final_s, booked)
            if dkey in dedupe:
                continue
            dedupe.add(dkey)

            parent_key = f"{final_g}|{final_s}|{final_tc}"
            group_name = group_desc.get(final_g, "")
            subgroup_name = subgroup_desc.get((final_g, final_s), "")

            group_item = groups_map.setdefault(
                final_g,
                {
                    "group_name": group_name,
                    "_group_code": final_g,
                    "_subgroups": {},
                },
            )
            subgroup_map = group_item["_subgroups"]
            subgroup_item = subgroup_map.setdefault(
                final_s,
                {
                    "subgroup_name": subgroup_name,
                    "_subgroup_code": final_s,
                    "tests": [],
                },
            )

            subgroup_item["tests"].append(
                {
                    "booked_code": booked,
                    "description": final_desc,
                    "is_profile": bool(meta.is_profile) if meta else False,
                    "has_children": (final_g, final_s, final_tc) in profile_children_keys,
                    "charge": self._to_number(row.Charge),
                    "mrp": self._to_number(row.MRP),
                    "max_discount": self._to_number(row.MaxDiscount),
                    "child_tests": profile_children_map.get(parent_key, []),
                }
            )

        groups: list[dict[str, Any]] = []
        for group_code, group_item in sorted(groups_map.items(), key=lambda x: x[0]):
            subgroups: list[dict[str, Any]] = []
            for subgroup_code, subgroup_item in sorted(
                group_item["_subgroups"].items(),
                key=lambda x: x[0],
            ):
                subgroup_item["tests"].sort(
                    key=lambda x: (x["description"].lower(), x["booked_code"])
                )
                subgroups.append(
                    {
                        "subgroup_name": subgroup_item["subgroup_name"],
                        "tests": subgroup_item["tests"],
                    }
                )
            groups.append({"group_name": group_item["group_name"], "subgroups": subgroups})

        return {
            "ok": True,
            "panel_company": selected_company,
            "groups": groups,
        }
