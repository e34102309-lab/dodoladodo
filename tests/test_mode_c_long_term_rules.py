import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODE_C_PATH = ROOT / "AQR_ModeC_Agent_V12.py"
AI_PATH = ROOT / "run_mode_c_ai_agent.py"


class ModeCLongTermRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mode_c = MODE_C_PATH.read_text(encoding="utf-8")
        cls.ai = AI_PATH.read_text(encoding="utf-8")

    def test_python_sources_parse(self):
        ast.parse(self.mode_c)
        ast.parse(self.ai)

    def test_quality_first_weighting_and_capital_allocation(self):
        self.assertIn("quality_score * 0.35", self.mode_c)
        self.assertIn("value_score * 0.30", self.mode_c)
        self.assertIn("expectations_score * 0.20", self.mode_c)
        self.assertIn("momentum_score * 0.10", self.mode_c)
        self.assertIn("r.Capital_Allocation_Score * 0.05", self.mode_c)

    def test_cash_flow_and_return_on_capital_metrics_exist(self):
        for marker in (
            "calculate_fcf_stability",
            "OCF_3Y_Cumulative_B",
            "Real_FCF_Positive_Years_5Y",
            "Real_FCF_Margin_Std_5Y_pct",
            "OCF_to_NetIncome_5Y",
            "Real_FCF_to_NetIncome_5Y",
            "ROIC_pct",
            "ROCE_pct",
        ):
            self.assertIn(marker, self.mode_c)

    def test_single_year_dilution_is_warning_but_three_year_is_hard_rule(self):
        self.assertIn("risk_penalty += 8.0", self.mode_c)
        self.assertIn("share_change_3y_pct > 3.0", self.mode_c)
        self.assertIn("and not r.Persistent_Dilution", self.mode_c)
        self.assertNotIn("and not r.Dilution_Illusion", self.mode_c)

    def test_buy_thresholds_and_etf_overlap_rule(self):
        self.assertIn("SMALL_POSITION_SCORE = 75.0", self.mode_c)
        self.assertIn("ETF_TOP10_MIN_BUY_SCORE = 80.0", self.mode_c)
        self.assertIn("qqq_or_voo_top_10", self.mode_c)
        self.assertIn("QQQ 或 VOO 前十大", self.ai)
        self.assertIn("一般股票 75 分", self.ai)

    def test_financials_are_formally_excluded_as_model_mismatch(self):
        self.assertIn("MODEL_EXCLUDED_SECTORS", self.mode_c)
        self.assertIn("金融股財務結構不適用本模型", self.mode_c)

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
