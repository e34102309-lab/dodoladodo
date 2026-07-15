import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODE_C_PATH = ROOT / "AQR_ModeC_Agent_V12.py"
AI_PATH = ROOT / "run_mode_c_ai_agent.py"
EVIDENCE_PATH = ROOT / "mode_c_evidence.py"
ROUTING_PATH = ROOT / "mode_c_routing.py"
INDUSTRY_MODELS_PATH = ROOT / "mode_c_industry_models.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "alpha_hunt.yml"


class ModeCLongTermRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mode_c = MODE_C_PATH.read_text(encoding="utf-8")
        cls.ai = AI_PATH.read_text(encoding="utf-8")
        cls.evidence = EVIDENCE_PATH.read_text(encoding="utf-8")
        cls.routing = ROUTING_PATH.read_text(encoding="utf-8")
        cls.industry_models = INDUSTRY_MODELS_PATH.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_python_sources_parse(self):
        ast.parse(self.mode_c)
        ast.parse(self.ai)
        ast.parse(self.evidence)
        ast.parse(self.routing)
        ast.parse(self.industry_models)

    def test_quality_first_weighting_and_capital_allocation(self):
        self.assertIn("quality_score * 0.35", self.mode_c)
        self.assertIn("value_score * 0.30", self.mode_c)
        self.assertIn("expectations_score * 0.20", self.mode_c)
        self.assertIn("momentum_score * 0.05", self.mode_c)
        self.assertIn("inflection_score * 0.05", self.mode_c)
        self.assertIn("r.Capital_Allocation_Score * 0.05", self.mode_c)

    def test_cash_flow_and_return_on_capital_metrics_exist(self):
        for marker in (
            "calculate_fcf_stability",
            "estimate_maintenance_capex_profile",
            "Maintenance_CapEx_B",
            "Growth_CapEx_B",
            "Conservative_Real_FCF_Yield_pct",
            "OCF_3Y_Cumulative_B",
            "Real_FCF_Positive_Years_5Y",
            "Real_FCF_Margin_Std_5Y_pct",
            "OCF_to_NetIncome_5Y",
            "Real_FCF_to_NetIncome_5Y",
            "ROIC_pct",
            "ROCE_pct",
            "Stress_ICR_30x",
            "Stress_Survival_30",
            "Inventory_Signal",
        ):
            self.assertIn(marker, self.mode_c)

    def test_single_year_dilution_is_warning_but_three_year_is_hard_rule(self):
        self.assertIn("risk_penalty += 5.0", self.mode_c)
        self.assertIn("share_change_3y_pct > 3.0", self.mode_c)
        self.assertIn("and not r.Persistent_Dilution", self.mode_c)
        self.assertNotIn("and not r.Dilution_Illusion", self.mode_c)

    def test_dynamic_cagr_limit_and_score_first_shortlist_exist(self):
        self.assertIn("dynamic_implied_cagr_limit", self.mode_c)
        self.assertIn("r.Expectations_Score >= 15.0", self.mode_c)
        self.assertIn("REVERSE_DCF_REQUIRED_RETURN", self.mode_c)
        self.assertNotIn("r.Implied_EBITDA_CAGR_3Y_pct <= r.Implied_CAGR_Limit_pct", self.mode_c)
        self.assertIn("MAX_PER_SECTOR = 0", self.mode_c)
        self.assertIn("if max_per_sector > 0", self.mode_c)

    def test_thursday_after_close_mode_c_and_monthly_universe_refresh_are_separate(self):
        self.assertIn("- cron: '0 22 * * 4'", self.workflow)
        self.assertIn("- cron: '0 3 1 * *'", self.workflow)
        self.assertGreaterEqual(
            self.workflow.count("github.event.schedule == '0 3 1 * *'"),
            4,
        )
        self.assertIn('contact_email="${USER_EMAIL:-a7924177@gmail.com}"', self.workflow)
        self.assertIn(
            'os.environ.get("USER_EMAIL") or "a7924177@gmail.com"',
            self.mode_c,
        )

    def test_insurance_route_is_not_changed_to_match_available_tags(self):
        self.assertNotIn("alternate_key =", self.mode_c)

    def test_buy_thresholds_and_etf_overlap_rule(self):
        self.assertIn("SMALL_POSITION_SCORE = 75.0", self.mode_c)
        self.assertIn("ETF_TOP10_MIN_BUY_SCORE = 80.0", self.mode_c)
        self.assertIn("qqq_or_voo_top_10", self.mode_c)
        self.assertIn("QQQ 或 VOO 前十大", self.ai)
        self.assertIn("一般股票 75 分", self.ai)

    def test_specialized_industries_have_explicit_models_and_abstain_gates(self):
        self.assertIn("route_industry_model", self.mode_c)
        self.assertIn('route = "BANK"', self.routing)
        self.assertIn('"route": "INSURANCE"', self.routing)
        self.assertIn('"route": "REIT"', self.routing)
        self.assertIn('"route": "CYCLICAL_MIDCYCLE"', self.routing)
        self.assertIn('Decision_State="ABSTAIN"', self.mode_c)
        for model_key in (
            "BANK",
            "INSURANCE_P_AND_C",
            "INSURANCE_LIFE",
            "REIT_EQUITY",
            "REIT_MORTGAGE",
            "REGULATED_UTILITY",
            "CYCLICAL_MIDCYCLE",
            "FINANCIAL_LENDER",
            "FINANCIAL_FEE",
        ):
            self.assertIn(f'"{model_key}"', self.industry_models)
        for marker in (
            "run_specialized_mode_c_pipeline",
            "assess_specialized_data_confidence",
            "Industry_Model_Metrics_JSON",
            "selected_source_evidence_ids",
            "SPECIALIZED_DISCLOSURE_GAPS",
        ):
            self.assertIn(marker, self.mode_c)
        self.assertIn("set(sbc)", self.mode_c)
        self.assertIn("minimum_positive_fcf_years", self.mode_c)
        self.assertIn("if not sbc_metric_evidence_id", self.mode_c)
        self.assertIn("cannot be treated as zero", self.mode_c)
        self.assertIn('("OCF", "CapEx", "DnA", "Revenue", "SBC")', self.mode_c)
        self.assertIn('evaluation.decision == "ABSTAIN"', self.mode_c)

    def test_specialized_recency_contract_only_references_fetched_concepts(self):
        tree = ast.parse(self.mode_c)

        def literal_assignment(name):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return ast.literal_eval(node.value)
            self.fail(f"Missing literal assignment: {name}")

        specialized = literal_assignment("SPECIALIZED_CONCEPTS")
        recency = literal_assignment("SPECIALIZED_RECENCY_CONCEPTS")
        config = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "config"
                ):
                    config = ast.literal_eval(node.value)
                    break
            if config is not None:
                break
        self.assertIsNotNone(config)
        self.assertEqual(set(specialized), set(recency))
        base_concepts = {
            "Assets",
            "Cash",
            "DebtCommercialPaper",
            "DebtCurrent",
            "DebtFinanceLease",
            "DebtOtherShortTerm",
            "DebtShortTermTotal",
            "DebtTotal",
            "Equity",
            "Goodwill",
            "IntangibleAssets",
            "NetIncome",
        }
        for model_key, metric_contract in recency.items():
            referenced = {
                concept
                for concepts in metric_contract.values()
                for concept in concepts
            }
            allowed = base_concepts | set(specialized[model_key])
            self.assertFalse(referenced - allowed, (model_key, referenced - allowed))
            self.assertFalse(allowed - set(config), (model_key, allowed - set(config)))

    def test_mode_c_result_calls_only_use_declared_fields(self):
        tree = ast.parse(self.mode_c)
        result_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ModeCResult"
        )
        declared = {
            node.target.id
            for node in result_class.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        unknown = set()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ModeCResult"
            ):
                continue
            unknown.update(
                keyword.arg for keyword in node.keywords
                if keyword.arg is not None and keyword.arg not in declared
            )
        self.assertFalse(unknown)

    def test_point_in_time_evidence_and_confidence_gate_exist(self):
        for marker in (
            "available_to_model_at",
            "accepted_at",
            "source_evidence_ids",
            "selected_for_model",
        ):
            self.assertIn(marker, self.evidence)
        self.assertIn("Data_Confidence_Score", self.mode_c)

    def test_one_decision_timestamp_is_shared_by_the_whole_run(self):
        self.assertIn("run_decision_timestamp = pd.Timestamp.now", self.mode_c)
        self.assertIn(
            "user_email,\n                run_decision_timestamp,",
            self.mode_c,
        )
        self.assertIn("and r.Data_Confidence_Score >= MIN_DATA_CONFIDENCE", self.mode_c)
        self.assertIn(
            "short_interest_data_age_days(info, now=decision_timestamp)",
            self.mode_c,
        )
        self.assertIn(
            "get_upcoming_earnings(ticker, info, now=decision_timestamp)",
            self.mode_c,
        )
        self.assertIn("self._thread_local = threading.local()", self.mode_c)
        self.assertIn("self._session().get", self.mode_c)

    def test_split_adjustment_and_selected_evidence_output_are_enforced(self):
        self.assertIn('Scoring_Framework: str = "GENERAL_CORPORATE_V13"', self.mode_c)
        self.assertIn("actions=True", self.mode_c)
        self.assertIn("split_adjusted_share_value", self.mode_c)
        self.assertIn("Share_Basis_Discontinuity", self.mode_c)
        self.assertIn("rows(selected_only=True)", self.mode_c)
        self.assertIn("Mode C 深篩進度", self.mode_c)

    def test_ai_has_fixed_nine_question_review(self):
        for marker in (
            "三句話投資論點",
            "最強反方",
            "thesis 失效條件",
            "悲觀/基準/樂觀",
            "最新財報警訊",
            "股數稀釋",
            "資本配置",
            "QQQ/VOO 重疊",
            "仍值得主動加碼的理由",
        ):
            self.assertIn(marker, self.ai)


if __name__ == "__main__":
    unittest.main()
