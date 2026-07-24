from sqlalchemy import bindparam
from sqlalchemy import text
from sqlalchemy.orm import Session


class PanelCatalogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def fetch_selected_panel_company_rows(self):
        return self.db.execute(
            text(
                """
                SELECT
                    a.CenterID,
                    a.pname,
                    a.category AS CompCatID,
                    a.BillingChargeMode,
                    COALESCE(a.showmrp, 0) AS showmrp,
                    COALESCE(a.showinHC, 0) AS showinHC,
                    c.CatDetails
                FROM address a
                LEFT JOIN compcategory c
                    ON c.CompCatID = a.category
                WHERE a.pname IS NOT NULL
                  AND TRIM(a.pname) <> ''
                  AND UPPER(TRIM(a.Atype)) = 'C'
                  AND COALESCE(a.Active, 0) = 1
                  AND COALESCE(a.showinHC, 0) = 1
                """
            )
        ).fetchall()

    def fetch_test_rows(self):
        return self.db.execute(
            text(
                """
                SELECT
                    Gcode,
                    Scode,
                    TestCode,
                    Testcode1,
                    Description,
                    Profile
                FROM test
                """
            )
        ).fetchall()

    def fetch_test_profile_rows(self):
        return self.db.execute(
            text(
                """
                SELECT Gcode, SCode, ProfileCode, TestCode
                FROM testprofile
                """
            )
        ).fetchall()

    def fetch_group_rows(self):
        return self.db.execute(
            text(
                """
                SELECT Gcode, Description
                FROM groupmaster
                """
            )
        ).fetchall()

    def fetch_subgroup_rows(self):
        return self.db.execute(
            text(
                """
                SELECT Gcode, Scode, Description
                FROM subgroup
                """
            )
        ).fetchall()

    def fetch_panel_rate_rows_for_comp_id(self, comp_id: str):
        return self.db.execute(
            text(
                """
                SELECT
                    CompCatID,
                    GCode,
                    SCode,
                    TestCode,
                    CTestCode,
                    CTestName,
                    Charge,
                    MRP,
                    MaxDiscount
                FROM panelrates
                WHERE BookedFlag = 1
                  AND COALESCE(Active, 0) = 1
                  AND CompCatID = :comp_id
                """
            ),
            {"comp_id": comp_id},
        ).fetchall()

    def fetch_panel_rate_rows_for_comp_ids(self, comp_ids: list[str]):
        if not comp_ids:
            return []
        stmt = text(
            """
            SELECT
                CompCatID,
                GCode,
                SCode,
                TestCode,
                CTestCode,
                CTestName,
                Charge,
                MRP,
                MaxDiscount
            FROM panelrates
            WHERE BookedFlag = 1
              AND COALESCE(Active, 0) = 1
              AND CompCatID IN :comp_ids
            """
        ).bindparams(bindparam("comp_ids", expanding=True))
        return self.db.execute(stmt, {"comp_ids": comp_ids}).fetchall()
