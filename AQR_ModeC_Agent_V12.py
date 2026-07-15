"""
=============================================================================
AQR Mode-C Agent V13 — 70/30 長期價值研究框架
=============================================================================
用途：
1) 讀取每月全市場初篩清單 qualified_universe.csv（欄位：Ticker, CIK）
2) 使用 SEC XBRL Company Facts 對齊 TTM / 最新 10-K、20-F、40-F / 最新 10-Q
3) 產出：
   - mode_c_screen.csv              ：全量結構化數據總表
   - mode_c_shortlist.csv           ：分數優先的長期研究候選
   - mode_c_evidence_ledger.csv     ：point-in-time 原始證據與衍生血緣
   - mode_c_report.md               ：長期價值研究報告
   - mode_c_agent_payload.json      ：交給 LLM / Web Agent 做物理限制驗證的任務包

核心修正：
- 完美還原稅務利益 (Tax Benefit)，強制執行三點勾稽防止非經常性損益欺騙。
- 將軋空水位 (Short Interest & DTC) 強制寫入 CSV 警示旗標。
- 徹底阻絕科技巨頭 (無傳統債務) 造成的 KeyError 熔斷。
=============================================================================
"""


from __future__ import annotations


import concurrent.futures
import json
import logging
import math
import os
import re
import smtplib
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


import numpy as np
import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from mode_c_evidence import EVIDENCE_COLUMNS, GLOBAL_EVIDENCE_LEDGER
from mode_c_metric_contract import annotate_rows, finite_number
from mode_c_industry_models import (
    SPECIALIZED_MODEL_KEYS,
    IndustryModelEvaluation,
    assess_specialized_data_confidence,
    evaluate_industry_model,
)
from mode_c_routing import route_industry_model


# ==============================================================================
# 基本設定
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ModeC")


CACHE_FILE_SHARES = "local_shares_vector_cache.json"
QUALIFIED_UNIVERSE = "qualified_universe.csv"
OUTPUT_CSV = "mode_c_screen.csv"
OUTPUT_SHORTLIST_CSV = "mode_c_shortlist.csv"
OUTPUT_MD = "mode_c_report.md"
OUTPUT_JSON = "mode_c_agent_payload.json"
OUTPUT_EVIDENCE_CSV = "mode_c_evidence_ledger.csv"


# SEC Fair Access 官方上限是 10 req/s；這裡保守設 8。
SEC_MAX_CALLS_PER_SECOND = 8
SEC_TIMEOUT = 15
SEC_DATA_AVAILABILITY_LAG_MINUTES = 5
ANNUAL_FILING_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


# ==============================================================================
# QQQ 40% + VOO 30% + 最多 30% 主動選股：長期價值投資框架
# ==============================================================================
MIN_LIQUIDITY_USD = 15_000_000
MIN_MARKET_CAP_B = 5.0
ICR_WARNING = 3.0
STRESS_ICR_MIN = 1.5
REVERSE_DCF_REQUIRED_RETURN = 0.10
SHORT_SQUEEZE_SI = 15.0
SHORT_SQUEEZE_DTC = 5.0
LOW_VALUATION_PERCENTILE = 5
ACTIVE_SLEEVE_LIMIT_PCT = 30.0
TARGET_SHORTLIST_SIZE = 12
MAX_PER_SECTOR = 0
STARTER_WEIGHT_PCT_TOTAL = 1.5
MAX_POSITION_WEIGHT_PCT_TOTAL = 3.0
MAX_SECTOR_WEIGHT_PCT_TOTAL = 9.0
MIN_LONG_TERM_SCORE = 60.0
RESEARCH_PRIORITY_SCORE = 70.0
SMALL_POSITION_SCORE = 75.0
HIGH_PRIORITY_SCORE = 80.0
ETF_TOP10_MIN_BUY_SCORE = 80.0
STARTER_WEIGHT_MIN_PCT_TOTAL = 1.0
MIN_DATA_CONFIDENCE = 70.0
MAX_DOMESTIC_CORE_FACT_AGE_DAYS = 240
MAX_FOREIGN_ANNUAL_FACT_AGE_DAYS = 550
MAX_CORE_FACT_AGE_DAYS = MAX_FOREIGN_ANNUAL_FACT_AGE_DAYS


# ==============================================================================
# 快取
# ==============================================================================
_BULK_MARKET_DATA: Optional[pd.DataFrame] = None
_INFO_CACHE: Dict[str, dict] = {}
_RF_CACHE: Optional[float] = None




def load_json_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}




def save_json_cache(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)




_VECTOR_CACHE = load_json_cache(CACHE_FILE_SHARES)


# ==============================================================================
# HTTP / Yahoo
# ==============================================================================


def create_retry_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[401, 403, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "ModeCQuantResearch/12.0 contact@example.com",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session




def pre_fetch_all_market_data(
    tickers: List[str],
    period: str = "10y",
    batch_size: int = 100,
) -> None:
    global _BULK_MARKET_DATA
    tickers = sorted(set([t for t in tickers if t]))
    logger.info(f"批量下載價格資料：{len(tickers)} 檔，period={period}")
    frames = []
    size = max(1, int(batch_size))
    for start in range(0, len(tickers), size):
        batch = tickers[start : start + size]
        frame = yf.download(
            batch,
            period=period,
            actions=True,
            progress=False,
            auto_adjust=False,
            group_by="column",
            threads=True,
        )
        if frame is None or frame.empty:
            logger.warning("價格批次下載為空：%s", ",".join(batch[:5]))
            continue
        if not isinstance(frame.columns, pd.MultiIndex):
            if len(batch) != 1:
                logger.warning("多股票價格批次欄位格式異常，略過該批次")
                continue
            frame = frame.copy()
            frame.columns = pd.MultiIndex.from_tuples(
                [(str(column), batch[0]) for column in frame.columns]
            )
        frames.append(frame)
    _BULK_MARKET_DATA = (
        pd.concat(frames, axis=1).loc[:, lambda data: ~data.columns.duplicated()]
        if frames
        else pd.DataFrame()
    )




def get_cached_series(ticker: str, col: str) -> Optional[pd.Series]:
    if _BULK_MARKET_DATA is None or _BULK_MARKET_DATA.empty:
        return None
    try:
        if isinstance(_BULK_MARKET_DATA.columns, pd.MultiIndex):
            if (col, ticker) in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[(col, ticker)].dropna()
                return s if not s.empty else None
        else:
            if col in _BULK_MARKET_DATA.columns:
                s = _BULK_MARKET_DATA[col].dropna()
                return s if not s.empty else None
    except Exception:
        return None
    return None


def split_adjusted_share_value(
    ticker: str,
    value: float,
    fact_end: Any,
    fact_filed_at: Any,
    concept: str,
    target_date: Any,
) -> Tuple[float, float]:
    """Convert a reported share fact to the target-date split basis."""
    if not math.isfinite(float(value)) or float(value) <= 0:
        return float(value), 1.0
    splits = get_cached_series(ticker, "Stock Splits")
    if splits is None or splits.empty:
        return float(value), 1.0
    start = pd.to_datetime(fact_end, utc=True, errors="coerce")
    target = pd.to_datetime(target_date, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(target) or target <= start:
        return float(value), 1.0

    # Weighted-average share facts filed after a split are normally recast to
    # that filing-date basis. Instant share facts retain their period-end basis.
    filed = pd.to_datetime(fact_filed_at, utc=True, errors="coerce")
    is_weighted_average = "WeightedAverageNumberOfShares" in str(concept or "")
    basis_start = max(start, filed) if is_weighted_average and pd.notna(filed) else start
    events = splits.copy()
    events.index = pd.to_datetime(events.index, utc=True, errors="coerce")
    events = pd.to_numeric(events, errors="coerce")
    events = events[(events.index > basis_start) & (events.index <= target) & (events > 0)]
    events = events[~np.isclose(events, 1.0)]
    if events.empty:
        return float(value), 1.0
    factor = float(events.prod())
    if not math.isfinite(factor) or factor <= 0 or math.isclose(factor, 1.0):
        return float(value), 1.0

    return float(value) * factor, factor




def get_price_asof(ticker: str, date_like: pd.Timestamp) -> float:
    close = get_cached_series(ticker, "Close")
    if close is None or close.empty:
        return np.nan
    date_like = pd.Timestamp(date_like).tz_localize(None)
    s = close.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s[s.index <= date_like]
    if s.empty:
        return np.nan
    return float(s.iloc[-1])


def get_price_on_or_after(ticker: str, date_like: pd.Timestamp) -> float:
    close = get_cached_series(ticker, "Close")
    if close is None or close.empty:
        return np.nan
    date_like = pd.Timestamp(date_like).tz_localize(None).normalize()
    s = close.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    s = s[s.index >= date_like]
    return float(s.iloc[0]) if not s.empty else np.nan




def pre_fetch_all_info(tickers: List[str]) -> None:
    global _INFO_CACHE
    logger.info(f"批量抓取 yf.info：{len(tickers)} 檔")
    for i, t in enumerate(tickers, start=1):
        info_data = {}
        for attempt in range(2):
            try:
                info = yf.Ticker(t).info
                if isinstance(info, dict) and len(info) > 5:
                    info_data = dict(info)
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        _INFO_CACHE[t] = info_data
        if i % 50 == 0:
            logger.info(f"  yf.info {i}/{len(tickers)}")
        time.sleep(0.05)




def safe_yf_info(ticker: str) -> dict:
    info = _INFO_CACHE.get(ticker, {}) or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price:
        return info
    close = get_cached_series(ticker, "Close")
    if close is not None and not close.empty:
        info = dict(info)
        info.setdefault("currentPrice", float(close.iloc[-1]))
        info.setdefault("regularMarketPrice", float(close.iloc[-1]))
    return info


def hydrate_info_cache_from_verified_universe(df: pd.DataFrame) -> None:
    """Use monthly hunter identity metadata when a daily yf.info call is empty."""
    sec_to_yahoo_exchange = {
        "NASDAQ": "NMS",
        "NYSE": "NYQ",
        "NYSE AMERICAN": "ASE",
    }

    def text(value: Any) -> str:
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value or "").strip()

    for _, row in df.iterrows():
        if text(row.get("Status")).upper() != "PASS":
            continue
        ticker = text(row.get("Ticker")).upper()
        cik = text(row.get("CIK")).replace(".0", "").zfill(10)
        sec_exchange = text(row.get("SECExchange")).upper()
        if not ticker or not cik.isdigit() or int(cik) <= 0:
            continue
        info = dict(_INFO_CACHE.get(ticker) or {})
        quote_type = text(row.get("QuoteType")).upper()
        exchange = text(row.get("Exchange")).upper()
        if not quote_type and sec_exchange in sec_to_yahoo_exchange:
            quote_type = "EQUITY"
        if not exchange:
            exchange = sec_to_yahoo_exchange.get(sec_exchange, "")
        fallback_fields = []
        for key, value in (
            ("quoteType", quote_type),
            ("exchange", exchange),
            ("sector", text(row.get("Sector"))),
            ("industry", text(row.get("Industry"))),
        ):
            if value and not info.get(key):
                info[key] = value
                fallback_fields.append(key)
        if fallback_fields:
            info["_verifiedUniverseMetadataFallback"] = True
            info["_verifiedUniverseMetadataFallbackFields"] = fallback_fields
        _INFO_CACHE[ticker] = info


# ==============================================================================
# SEC XBRL 抽取器
# ==============================================================================
class RateLimitedSession:
    def __init__(self, calls: int = SEC_MAX_CALLS_PER_SECOND, period: float = 1.0):
        self.calls = calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: List[float] = []
        self._thread_local = threading.local()


    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = create_retry_session()
            self._thread_local.session = session
        return session


    def _wait_for_capacity(self) -> None:
        with self.lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.calls:
                sleep_time = self.period - (now - self.timestamps[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.timestamps.append(time.time())


    def get(self, url: str, headers: dict) -> Optional[requests.Response]:
        for attempt in range(5):
            self._wait_for_capacity()
            try:
                r = self._session().get(url, headers=headers, timeout=SEC_TIMEOUT)
                if r.status_code == 200:
                    return r
                if r.status_code == 404:
                    return None
                if r.status_code in (429, 503):
                    time.sleep((2 ** attempt) * 1.5)
            except requests.RequestException:
                time.sleep((2 ** attempt) * 1.2)
        return None




_GLOBAL_SEC_SESSION = RateLimitedSession()




class SECDataDistiller:
    def __init__(
        self,
        email: str,
        ticker: str = "",
        cik: str = "",
        decision_timestamp: Optional[pd.Timestamp] = None,
    ):
        self.headers = {"User-Agent": f"ModeCQuantResearch {email}"}
        self.session = _GLOBAL_SEC_SESSION
        self.ticker = str(ticker or "").upper()
        self.cik = str(cik or "").zfill(10) if str(cik or "") else ""
        self.decision_timestamp = self._utc_naive_timestamp(
            decision_timestamp if decision_timestamp is not None else pd.Timestamp.now(tz="UTC")
        )
        self.ledger = GLOBAL_EVIDENCE_LEDGER
        self._companyfacts_cache: Dict[str, dict] = {}
        self._submission_metadata_cache: Dict[str, Dict[str, dict]] = {}
        self.share_split_factors = {"current": 1.0, "1y": 1.0, "3y": 1.0}
        self.share_split_evidence_ids: List[str] = []
        self.config = {
            "OCF": [
                "NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ],
            "CapEx": [
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "PropertyPlantAndEquipmentAdditions",
            ],
            "SBC": [
                "ShareBasedCompensation",
                "StockBasedCompensation",
                "AllocatedShareBasedCompensationExpense",
                "ShareBasedCompensationExpense",
            ],
            "EBIT": [
                "OperatingIncomeLoss",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            ],
            "Interest": [
                "InterestExpense",
                "InterestExpenseDebt",
                "InterestExpenseNet",
                "InterestAndDebtExpense",
                "InterestPaidNet",
            ],
            "DnA": [
                "DepreciationDepletionAndAmortization",
                "DepreciationAndAmortization",
                "Depreciation",
            ],
            "Revenue": [
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet",
                "SalesRevenueGoodsNet",
            ],
            "GrossProfit": ["GrossProfit"],
            "COGS": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
            "Inventory": ["InventoryNet", "Inventory"],
            "DebtTotal": [
                "DebtCurrentAndLongTerm",
                "DebtAndFinanceLeaseObligations",
                "LongTermDebtAndFinanceLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebt",
                "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
                "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
                "LongTermDebtNoncurrent",
                "NotesPayable",
            ],
            "DebtCurrent": [
                "DebtCurrent",
                "LongTermDebtAndFinanceLeaseObligationsCurrent",
                "LongTermDebtAndCapitalLeaseObligationsCurrent",
                "LongTermDebtCurrent",
                "ShortTermBorrowings",
            ],
            "DebtShortTermTotal": ["ShortTermBorrowings"],
            "DebtOtherShortTerm": ["OtherShortTermBorrowings"],
            "DebtCommercialPaper": ["CommercialPaper"],
            "DebtFinanceLease": ["FinanceLeaseLiability"],
            "Cash": [
                "CashAndCashEquivalentsAtCarryingValue",
                "Cash",
            ],
            "Equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
            "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
            "Buyback": ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
            "StockIssuance": ["ProceedsFromIssuanceOfCommonStock", "StockIssuedDuringPeriodValueNewIssues"],
            "Dividend": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
            "EPSDiluted": ["EarningsPerShareDiluted"],
            "SharesDiluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
            "IncomeTaxExpenseBenefit": ["IncomeTaxExpenseBenefit", "CurrentIncomeTaxExpenseBenefit"],
            "Assets": ["Assets"],
            "Goodwill": ["Goodwill"],
            "IntangibleAssets": [
                "IntangibleAssetsNetExcludingGoodwill",
                "OtherIntangibleAssetsNet",
                "FiniteLivedIntangibleAssetsNet",
                "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
            ],
            "AOCI": ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
            "NetInterestIncome": ["InterestIncomeExpenseNet"],
            "Deposits": ["Deposits", "DepositsDomestic"],
            "Loans": [
                "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss",
                "LoansAndLeasesReceivableNetOfDeferredIncome",
                "LoansAndLeasesReceivableNetReportedAmount",
                "LoansReceivableNet",
            ],
            "CreditLossAllowance": [
                "FinancingReceivableAllowanceForCreditLossExcludingAccruedInterest",
                "FinancingReceivableAllowanceForCreditLosses",
                "LoansAndLeasesReceivableAllowance",
            ],
            "CreditLossProvision": [
                "ProvisionForLoanLeaseAndOtherLosses",
                "ProvisionForLoanAndLeaseLosses",
                "ProvisionForLoanLossesExpensed",
            ],
            "Tier1Ratio": ["TierOneRiskBasedCapitalToRiskWeightedAssets"],
            "Tier1WellCapitalizedMinimum": [
                "TierOneRiskBasedCapitalRequiredToBeWellCapitalizedToRiskWeightedAssets"
            ],
            "PremiumsEarned": [
                "PremiumsEarnedNet",
                "PremiumsEarnedNetPropertyAndCasualty",
                "SupplementaryInsuranceInformationPremiumRevenue",
            ],
            "PremiumsWritten": [
                "PremiumsWrittenNet",
                "SupplementaryInsuranceInformationPremiumsWritten",
            ],
            "InsuranceClaims": [
                "IncurredClaimsPropertyCasualtyAndLiability",
                "SupplementaryInsuranceInformationBenefitsClaimsLossesAndSettlementExpense",
            ],
            "InsuranceCombinedExpense": ["BenefitsLossesAndExpenses"],
            "UnderwritingExpense": [
                "OtherUnderwritingExpense",
                "DeferredPolicyAcquisitionCostAmortizationExpense",
                "SupplementaryInsuranceInformationAmortizationOfDeferredPolicyAcquisitionCosts",
            ],
            "ReserveDevelopment": [
                "LiabilityForUnpaidClaimsAndClaimsAdjustmentExpenseIncurredClaimsPriorYears"
            ],
            "PolicyholderBenefits": [
                "PolicyholderBenefitsAndClaimsIncurredNet",
                "BenefitsLossesAndExpenses",
            ],
            "NetInvestmentIncome": [
                "NetInvestmentIncome",
                "SupplementaryInsuranceInformationNetInvestmentIncome",
            ],
            "InsuranceReserves": [
                "LiabilityForClaimsAndClaimsAdjustmentExpense",
                "ReserveForLossesAndLossAdjustmentExpenses",
                "SupplementaryInsuranceInformationLiabilityForFuturePolicyBenefitsLossesClaimsAndLossExpenseReserves",
            ],
            "GainOnPropertySale": [
                "GainLossOnSaleOfProperties",
                "GainLossOnSaleOfPropertyPlantEquipment",
                "GainLossOnSaleOfRealEstate",
            ],
            "RealEstateImpairment": [
                "ImpairmentOfRealEstate",
                "ImpairmentOfLongLivedAssetsToBeDisposedOf",
                "AssetImpairmentCharges",
            ],
            "LeaseRevenue": [
                "OperatingLeasesIncomeStatementLeaseRevenue",
                "LeaseIncome",
                "RentalIncome",
                "RealEstateRevenueNet",
            ],
            "RealEstateCapEx": [
                "PaymentsForCapitalImprovements",
            ],
            "PPENet": [
                "PropertyPlantAndEquipmentNet",
                "RealEstateInvestmentPropertyNet",
                "RealEstateInvestments",
            ],
            "ConstructionWorkInProgress": [
                "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetBeforeAccumulatedDepreciationAndAmortizationConstructionInProgress",
                "ConstructionInProgressGross",
            ],
        }


    @staticmethod
    def _utc_naive_timestamp(value) -> pd.Timestamp:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return pd.NaT
        return pd.Timestamp(parsed).tz_convert(None)


    @staticmethod
    def _columnar_submission_rows(payload: dict) -> List[dict]:
        accessions = payload.get("accessionNumber") or []
        rows = []
        for index, accession in enumerate(accessions):
            row = {}
            for key in ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form"]:
                values = payload.get(key) or []
                row[key] = values[index] if index < len(values) else ""
            rows.append(row)
        return rows


    def _fetch_submission_metadata(self, cik: str) -> Dict[str, dict]:
        normalized_cik = str(cik).zfill(10)
        if normalized_cik in self._submission_metadata_cache:
            return self._submission_metadata_cache[normalized_cik]
        url = f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"
        response = self.session.get(url, headers=self.headers)
        if not response:
            self._submission_metadata_cache[normalized_cik] = {}
            return {}
        try:
            payload = response.json()
        except Exception:
            payload = {}
        filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
        rows = self._columnar_submission_rows(filings.get("recent", {}))
        for descriptor in filings.get("files", []) or []:
            name = str(descriptor.get("name") or "").strip()
            if not name:
                continue
            historical = self.session.get(f"https://data.sec.gov/submissions/{name}", headers=self.headers)
            if not historical:
                continue
            try:
                old_payload = historical.json()
            except Exception:
                continue
            old_rows = old_payload.get("filings", {}).get("recent", {}) if "filings" in old_payload else old_payload
            rows.extend(self._columnar_submission_rows(old_rows))

        metadata: Dict[str, dict] = {}
        for row in rows:
            accession = str(row.get("accessionNumber") or "").strip()
            if not accession:
                continue
            accepted_at = self._utc_naive_timestamp(row.get("acceptanceDateTime"))
            metadata[accession] = {
                "accepted_at": accepted_at,
                "filing_date": self._utc_naive_timestamp(row.get("filingDate")),
                "form": str(row.get("form") or ""),
            }
        self._submission_metadata_cache[normalized_cik] = metadata
        return metadata


    def _attach_point_in_time_evidence(
        self,
        df: pd.DataFrame,
        normalized_metric: str,
        unit: str,
    ) -> pd.DataFrame:
        if df.empty or not self.ticker or not self.cik:
            return df
        metadata = self._fetch_submission_metadata(self.cik)
        enriched = df.copy()
        evidence_ids = []
        accepted_values = []
        available_values = []
        availability_sources = []
        available_flags = []
        for _, row in enriched.iterrows():
            accession = str(row.get("accn") or "").strip()
            submission = metadata.get(accession, {})
            accepted_at = submission.get("accepted_at", pd.NaT)
            filed_at = self._utc_naive_timestamp(row.get("filed"))
            if pd.notna(accepted_at):
                available_at = pd.Timestamp(accepted_at) + pd.Timedelta(minutes=SEC_DATA_AVAILABILITY_LAG_MINUTES)
                availability_source = "accepted_at"
            elif pd.notna(filed_at):
                available_at = pd.Timestamp(filed_at).normalize() + pd.Timedelta(days=1)
                availability_source = "filed_date_fallback"
            else:
                available_at = pd.NaT
                availability_source = "missing"
            is_available = bool(
                pd.notna(available_at)
                and pd.notna(self.decision_timestamp)
                and available_at <= self.decision_timestamp
            )
            evidence_id = self.ledger.register_source_fact(
                ticker=self.ticker,
                cik=self.cik,
                source_system="SEC",
                concept=str(row.get("concept") or ""),
                normalized_metric=normalized_metric,
                value=row.get("val"),
                unit=unit,
                period_start=row.get("start"),
                period_end=row.get("end"),
                filed_at=filed_at,
                accepted_at=accepted_at,
                accession_number=accession,
                form=str(row.get("form") or ""),
                fy=row.get("fy"),
                fp=str(row.get("fp") or ""),
                source_priority=int(row.get("concept_priority") or 0),
                availability_source=availability_source,
                available_to_model_at=available_at,
                decision_timestamp=self.decision_timestamp,
                is_available_at_decision=is_available,
            )
            evidence_ids.append(evidence_id)
            accepted_values.append(accepted_at)
            available_values.append(available_at)
            availability_sources.append(availability_source)
            available_flags.append(is_available)
        enriched["evidence_id"] = evidence_ids
        enriched["accepted_at"] = accepted_values
        enriched["available_to_model_at"] = available_values
        enriched["availability_source"] = availability_sources
        enriched["is_available_at_decision"] = available_flags
        return enriched[enriched["is_available_at_decision"]].reset_index(drop=True)


    def _mark_rows_used(self, rows, role: str) -> List[str]:
        if rows is None:
            return []
        if isinstance(rows, pd.Series):
            ids = [str(rows.get("evidence_id") or "")]
        elif isinstance(rows, pd.DataFrame) and "evidence_id" in rows.columns:
            ids = [str(item) for item in rows["evidence_id"].dropna().tolist()]
        else:
            ids = []
        ids = [item for item in ids if item]
        self.ledger.mark_used(ids, role)
        return ids


    def _record_derived_metric(
        self,
        normalized_metric: str,
        value: float,
        unit: str,
        formula: str,
        source_rows,
        role: str,
    ) -> str:
        if not normalized_metric or not self.ticker or not self.cik:
            return ""
        source_ids: List[str] = []
        for rows in source_rows:
            source_ids.extend(self._mark_rows_used(rows, role))
        return self.ledger.register_derived_metric(
            ticker=self.ticker,
            cik=self.cik,
            normalized_metric=normalized_metric,
            value=value,
            unit=unit,
            formula=formula,
            source_evidence_ids=source_ids,
            decision_timestamp=self.decision_timestamp,
            role=role,
        )


    def _record_derived_from_ids(
        self,
        normalized_metric: str,
        value: float,
        unit: str,
        formula: str,
        source_evidence_ids: Iterable[str],
        role: str,
    ) -> str:
        if not normalized_metric or not self.ticker or not self.cik:
            return ""
        return self.ledger.register_derived_metric(
            ticker=self.ticker,
            cik=self.cik,
            normalized_metric=normalized_metric,
            value=value,
            unit=unit,
            formula=formula,
            source_evidence_ids=source_evidence_ids,
            decision_timestamp=self.decision_timestamp,
            role=role,
        )


    def record_observed_input(
        self,
        normalized_metric: str,
        value,
        unit: str,
        source_system: str,
        concept: str,
        period_end=None,
    ) -> str:
        if not self.ticker or not self.cik:
            return ""
        evidence_id = self.ledger.register_source_fact(
            ticker=self.ticker,
            cik=self.cik,
            source_system=source_system,
            concept=concept,
            normalized_metric=normalized_metric,
            value=value,
            unit=unit,
            period_start="",
            period_end=period_end if period_end is not None else self.decision_timestamp,
            filed_at="",
            accepted_at="",
            accession_number="",
            form="",
            fy="",
            fp="",
            source_priority=0,
            availability_source="observed_at_run",
            available_to_model_at=self.decision_timestamp,
            decision_timestamp=self.decision_timestamp,
            is_available_at_decision=True,
        )
        self.ledger.mark_used([evidence_id], f"{normalized_metric}:model-input")
        return evidence_id


    def _fetch_companyfacts(self, cik: str) -> dict:
        normalized_cik = str(cik).zfill(10)
        if normalized_cik in self._companyfacts_cache:
            return self._companyfacts_cache[normalized_cik]
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{normalized_cik}.json"
        r = self.session.get(url, headers=self.headers)
        if not r:
            self._companyfacts_cache[normalized_cik] = {}
            return {}
        try:
            payload = r.json().get("facts", {})
        except Exception:
            payload = {}
        self._companyfacts_cache[normalized_cik] = payload
        return payload


    def fetch_concept(self, cik: str, concept: str, units: Tuple[str, ...] = ("USD",)) -> pd.DataFrame:
        tags = self.config.get(concept, [concept])
        taxonomy = self._fetch_companyfacts(cik).get("us-gaap", {})
        frames = []
        for priority, tag in enumerate(tags):
            fact = taxonomy.get(tag, {})
            unit_map = fact.get("units", {})
            for unit in units:
                rows = unit_map.get(unit, [])
                if not rows:
                    continue
                try:
                    df = pd.DataFrame(rows)
                    df["concept"] = tag
                    df["concept_priority"] = priority
                    df["unit"] = unit
                    cleaned = self._clean_facts(df)
                    cleaned = self._attach_point_in_time_evidence(cleaned, concept, unit)
                    if not cleaned.empty:
                        frames.append(cleaned)
                except Exception:
                    continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False)


    def fetch_shares_outstanding(self, cik: str) -> pd.DataFrame:
        tag_candidates = [
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStocksIncludingAdditionalPaidInCapitalSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
            ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
        ]
        companyfacts = self._fetch_companyfacts(cik)
        frames = []
        for priority, (taxonomy, tag) in enumerate(tag_candidates):
            fact = companyfacts.get(taxonomy, {}).get(tag, {})
            rows = fact.get("units", {}).get("shares", [])
            if not rows:
                continue
            try:
                df = pd.DataFrame(rows)
                df["concept"] = tag
                df["concept_priority"] = priority
                df["unit"] = "shares"
                cleaned = self._clean_facts(df)
                cleaned = self._attach_point_in_time_evidence(cleaned, "SharesOutstanding", "shares")
                if not cleaned.empty:
                    frames.append(cleaned)
            except Exception:
                continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False)


    @staticmethod
    def _clean_facts(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        for c in ["start", "end", "filed"]:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        if "start" in df.columns and "end" in df.columns:
            df["duration_days"] = (df["end"] - df["start"]).dt.days
        else:
            df["duration_days"] = np.nan
        if "form" not in df.columns:
            df["form"] = ""
        if "fp" not in df.columns:
            df["fp"] = ""
        if "fy" not in df.columns:
            df["fy"] = np.nan
        df["fy"] = pd.to_numeric(df["fy"], errors="coerce").astype("Int64")
        df["val"] = pd.to_numeric(df["val"], errors="coerce")
        df = df.dropna(subset=["val", "end"])
        sort_cols = [c for c in ["end", "filed"] if c in df.columns]
        df = df.sort_values(sort_cols)
        dedup = [c for c in ["fy", "fp", "form", "end", "duration_days", "frame", "accn"] if c in df.columns]
        if "accn" not in df.columns and "filed" in df.columns:
            dedup.append("filed")
        if dedup:
            df = df.drop_duplicates(subset=dedup, keep="last")
        return df.reset_index(drop=True)


    @staticmethod
    def _annual_facts(df: pd.DataFrame, latest_filed: bool = True) -> pd.DataFrame:
        if df.empty:
            return df
        d = df.copy()
        is_annual_form = d["form"].astype(str).str.upper().isin(ANNUAL_FILING_FORMS)
        is_annual_duration = d["duration_days"].between(330, 380, inclusive="both")
        annual = d[
            is_annual_form
            & (is_annual_duration | d["fp"].astype(str).str.upper().eq("FY"))
        ]
        if annual.empty:
            annual = d[is_annual_form]
        if annual.empty:
            return annual
        annual = annual.copy()
        annual["_duration_score"] = (annual["duration_days"] - 365).abs().fillna(0.0)
        if "concept_priority" not in annual.columns:
            annual["concept_priority"] = 0
        sort_cols = ["end", "_duration_score", "concept_priority"]
        ascending = [True, True, True]
        if "filed" in annual.columns:
            sort_cols.append("filed")
            ascending.append(not latest_filed)
        annual = annual.sort_values(sort_cols, ascending=ascending)
        annual = annual.drop_duplicates(subset=["end"], keep="first")
        return annual.drop(columns=["_duration_score"], errors="ignore").sort_values("end").reset_index(drop=True)


    @staticmethod
    def _instant_facts(df: pd.DataFrame, latest_filed: bool = True) -> pd.DataFrame:
        if df.empty:
            return df.copy()
        d = df.copy()
        if "concept_priority" not in d.columns:
            d["concept_priority"] = 0
        sort_cols = ["end", "concept_priority"]
        ascending = [True, True]
        filing_order_column = (
            "available_to_model_at"
            if "available_to_model_at" in d.columns
            else "filed"
            if "filed" in d.columns
            else ""
        )
        if filing_order_column:
            sort_cols.append(filing_order_column)
            ascending.append(not latest_filed)
        d = d.sort_values(sort_cols, ascending=ascending)
        d = d.drop_duplicates(subset=["end"], keep="first")
        return d.sort_values("end").reset_index(drop=True)


    @staticmethod
    def _instant_series(df: pd.DataFrame) -> pd.Series:
        d = SECDataDistiller._instant_facts(df)
        if d.empty:
            return pd.Series(dtype=float)
        return pd.Series(d["val"].to_numpy(dtype=float), index=pd.to_datetime(d["end"])).sort_index()


    @staticmethod
    def _instant_latest(df: pd.DataFrame) -> float:
        series = SECDataDistiller._instant_series(df)
        return float(series.iloc[-1]) / 1e9 if not series.empty else np.nan


    def latest_balance(self, df: pd.DataFrame, normalized_metric: str = "") -> float:
        facts = self._instant_facts(df)
        if facts.empty:
            return np.nan
        row = facts.iloc[-1]
        self._mark_rows_used(row, f"{normalized_metric}:latest-balance")
        return float(row["val"]) / 1e9


    def latest_balance_with_concept(
        self,
        df: pd.DataFrame,
        normalized_metric: str = "",
    ) -> Tuple[float, str]:
        facts = self._instant_facts(df)
        if facts.empty:
            return np.nan, ""
        row = facts.iloc[-1]
        self._mark_rows_used(row, f"{normalized_metric}:latest-balance")
        return float(row["val"]) / 1e9, str(row.get("concept") or "")


    def latest_annual(self, df: pd.DataFrame) -> Tuple[float, str]:
        annual = self._annual_facts(df)
        if annual.empty:
            return np.nan, "missing"
        r = annual.iloc[-1]
        form = str(r.get("form") or "annual filing").upper()
        return float(r["val"]) / 1e9, f"{form} {r.get('fy', '')} end={pd.Timestamp(r['end']).date()}"


    @staticmethod
    def _select_ytd(df: pd.DataFrame, fy, fp: str) -> Optional[pd.Series]:
        if df.empty or pd.isna(fy):
            return None
        fp = str(fp).upper()
        expected = {"Q1": 90, "Q2": 180, "Q3": 270, "FY": 365}.get(fp)
        d = df[(df["fy"].astype(str) == str(fy)) & (df["fp"].astype(str).str.upper() == fp)].copy()
        if d.empty:
            return None
        if expected:
            d["score"] = (d["duration_days"].fillna(expected) - expected).abs()
            if fp == "Q2":
                d = d[d["duration_days"].fillna(180).between(140, 220, inclusive="both")]
            elif fp == "Q3":
                d = d[d["duration_days"].fillna(270).between(230, 310, inclusive="both")]
            elif fp == "Q1":
                d = d[d["duration_days"].fillna(90).between(60, 130, inclusive="both")]
        if d.empty:
            return None
        # SEC facts include comparative periods from later filings. Restricting to
        # the latest period end prevents a prior-year comparative fact from being
        # mistaken for the current YTD value.
        d = d[d["end"] == d["end"].max()].copy()
        best_score = d["score"].min()
        d = d[d["score"] == best_score]
        if "concept_priority" in d.columns:
            best_priority = d["concept_priority"].min()
            d = d[d["concept_priority"] == best_priority]
        if "filed" in d.columns:
            d = d.sort_values("filed")
        return d.iloc[-1]


    def ttm_flow(
        self,
        df: pd.DataFrame,
        signed: bool = True,
        normalized_metric: str = "",
    ) -> Tuple[float, str, Dict[str, object]]:
        if df.empty:
            return np.nan, "missing", {}
        d = df.copy().sort_values(["end", "filed"] if "filed" in df.columns else ["end"])
        annual = self._annual_facts(d)
        q = d[d["form"].astype(str).str.upper().isin(["10-Q", "10-Q/A"])].copy()


        latest_10k_end = annual["end"].max() if not annual.empty else pd.NaT
        latest_10q_end = q["end"].max() if not q.empty else pd.NaT


        if pd.notna(latest_10k_end) and (pd.isna(latest_10q_end) or latest_10k_end >= latest_10q_end):
            r = annual.sort_values("end").iloc[-1]
            val = float(r["val"]) / 1e9
            result = val if signed else abs(val)
            evidence_id = self._record_derived_metric(
                normalized_metric,
                result,
                "USD_B",
                "latest annual filing value",
                [r],
                f"{normalized_metric}:ttm",
            )
            annual_form = str(r.get("form") or "annual filing").upper()
            fallback_label = (
                "fallback annual: "
                if annual_form.startswith(("20-F", "40-F"))
                else ""
            )
            return result, f"{fallback_label}TTM=latest {annual_form} end={pd.Timestamp(r['end']).date()}", {
                "annual": val,
                "evidence_id": evidence_id,
            }


        if pd.notna(latest_10q_end) and not annual.empty:
            latest_q_candidates = q[q["end"] == latest_10q_end]
            latest_q = latest_q_candidates.iloc[-1]
            fy, fp = latest_q.get("fy"), str(latest_q.get("fp", "")).upper()
            latest_ytd = self._select_ytd(d, fy, fp)
            prior_ytd = self._select_ytd(d, int(fy) - 1 if str(fy).isdigit() else fy, fp)
            annual_before = annual[annual["end"] < latest_q["end"]]
            if latest_ytd is not None and prior_ytd is not None and not annual_before.empty:
                ann = float(annual_before.iloc[-1]["val"])
                ly = float(latest_ytd["val"])
                py = float(prior_ytd["val"])
                ttm = (ann + ly - py) / 1e9
                result = ttm if signed else abs(ttm)
                annual_row = annual_before.iloc[-1]
                evidence_id = self._record_derived_metric(
                    normalized_metric,
                    result,
                    "USD_B",
                    f"annual + latest {fp} YTD - prior {fp} YTD",
                    [annual_row, latest_ytd, prior_ytd],
                    f"{normalized_metric}:ttm",
                )
                return result, (
                    f"TTM=annual filing + latest {fp} YTD - prior {fp} YTD; "
                    f"latest_end={pd.Timestamp(latest_q['end']).date()}"
                ), {
                    "annual": ann / 1e9,
                    "latest_ytd": ly / 1e9,
                    "prior_ytd": py / 1e9,
                    "evidence_id": evidence_id,
                }


        qs = self.quarterly_series(df, normalized_metric)
        if len(qs) >= 4:
            ttm = float(qs.tail(4).sum()) / 1e9
            result = ttm if signed else abs(ttm)
            source_ids = qs.attrs.get("source_evidence_ids", [])
            source_frame = (
                df[df["evidence_id"].isin(source_ids)]
                if source_ids and "evidence_id" in df.columns
                else df.iloc[0:0]
            )
            evidence_id = self._record_derived_metric(
                normalized_metric,
                result,
                "USD_B",
                "sum(last 4 derived quarters)",
                [source_frame],
                f"{normalized_metric}:ttm-fallback",
            )
            return result, "TTM=fallback sum(last 4 derived quarters)", {
                "q4sum": ttm,
                "evidence_id": evidence_id,
            }


        val, method = self.latest_annual(df)
        if not math.isfinite(val) or method == "missing":
            return np.nan, "missing", {}
        result = val if signed else abs(val)
        annual_rows = self._annual_facts(df).tail(1)
        evidence_id = self._record_derived_metric(
            normalized_metric,
            result,
            "USD_B",
            "fallback to latest annual filing value",
            [annual_rows],
            f"{normalized_metric}:ttm-annual-fallback",
        )
        return result, f"fallback annual: {method}", {"annual": val, "evidence_id": evidence_id}


    def quarterly_series(self, df: pd.DataFrame, normalized_metric: str = "") -> pd.Series:
        if df.empty:
            return pd.Series(dtype=float)
        d = df.copy()
        d = d[
            d["form"].astype(str).str.upper().isin(
                {"10-Q", "10-Q/A", *ANNUAL_FILING_FORMS}
            )
        ]
        if d.empty or "fy" not in d.columns or "fp" not in d.columns:
            return pd.Series(dtype=float)
        years = sorted([y for y in d["fy"].dropna().unique() if str(y).replace(".", "").isdigit()])
        out: List[Tuple[pd.Timestamp, float]] = []
        source_ids = set()
        for fy in years:
            q1 = self._select_ytd(d, fy, "Q1")
            q2 = self._select_ytd(d, fy, "Q2")
            q3 = self._select_ytd(d, fy, "Q3")
            fyv = self._select_ytd(d, fy, "FY")
            if fyv is None:
                annual = self._annual_facts(d[d["fy"].astype(str) == str(fy)])
                fyv = annual.iloc[-1] if not annual.empty else None
            q_vals = []
            if q1 is not None:
                q_vals.append((pd.Timestamp(q1["end"]), float(q1["val"])))
                source_ids.update(self._mark_rows_used(q1, f"{normalized_metric}:quarterly-series"))
            if q2 is not None and q1 is not None:
                q_vals.append((pd.Timestamp(q2["end"]), float(q2["val"]) - float(q1["val"])))
                source_ids.update(self._mark_rows_used(q2, f"{normalized_metric}:quarterly-series"))
            if q3 is not None and q2 is not None:
                q_vals.append((pd.Timestamp(q3["end"]), float(q3["val"]) - float(q2["val"])))
                source_ids.update(self._mark_rows_used(q3, f"{normalized_metric}:quarterly-series"))
            if fyv is not None and q3 is not None:
                q_vals.append((pd.Timestamp(fyv["end"]), float(fyv["val"]) - float(q3["val"])))
                source_ids.update(self._mark_rows_used(fyv, f"{normalized_metric}:quarterly-series"))
            for end, val in q_vals:
                if math.isfinite(val):
                    out.append((end, val))
        if not out:
            return pd.Series(dtype=float)
        s = pd.Series({end: val for end, val in out}).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        s.attrs["source_evidence_ids"] = sorted(source_ids)
        return s


    def get_shares_now_1y_3y(self, df: pd.DataFrame) -> Tuple[float, float, float]:
        facts = self._instant_facts(df)
        if facts.empty:
            return 0.0, 0.0, 0.0
        facts = facts.sort_values("end").reset_index(drop=True)
        latest_row = facts.iloc[-1]
        latest_end = pd.Timestamp(latest_row["end"])
        fact_age_days = (
            latest_end - pd.to_datetime(facts["end"], errors="coerce")
        ).dt.days
        old_1y = facts[fact_age_days.between(300, 450, inclusive="both")]
        old_3y = facts[fact_age_days.between(900, 1200, inclusive="both")]
        target_date = self.decision_timestamp if pd.notna(self.decision_timestamp) else latest_end

        def adjusted(row: Optional[pd.Series], bucket: str) -> float:
            if row is None:
                return 0.0
            adjusted_value, factor = split_adjusted_share_value(
                self.ticker,
                float(row["val"]),
                row.get("end"),
                row.get("available_to_model_at", row.get("filed")),
                str(row.get("concept") or ""),
                target_date,
            )
            self.share_split_factors[bucket] = factor
            if not math.isclose(factor, 1.0):
                evidence_id = self.record_observed_input(
                    f"Share_Split_Factor_{bucket}",
                    factor,
                    "ratio",
                    "YAHOO_FINANCE",
                    "Stock Splits",
                    period_end=target_date,
                )
                if evidence_id and evidence_id not in self.share_split_evidence_ids:
                    self.share_split_evidence_ids.append(evidence_id)
            return adjusted_value / 1e9

        now = adjusted(latest_row, "current")
        one_year_row = old_1y.iloc[-1] if not old_1y.empty else None
        three_year_row = old_3y.iloc[-1] if not old_3y.empty else None
        one_year_val = adjusted(one_year_row, "1y")
        three_year_val = adjusted(three_year_row, "3y")
        selected_ends = [latest_end]
        if not old_1y.empty:
            selected_ends.append(pd.Timestamp(one_year_row["end"]))
        if not old_3y.empty:
            selected_ends.append(pd.Timestamp(three_year_row["end"]))
        self._mark_rows_used(
            facts[facts["end"].isin(selected_ends)],
            "SharesOutstanding:current-and-history",
        )
        return now, one_year_val, three_year_val


    def get_shares_now_and_1y(self, df: pd.DataFrame) -> Tuple[float, float]:
        now, one_year_val, _ = self.get_shares_now_1y_3y(df)
        return now, one_year_val


    def shares_asof(self, df: pd.DataFrame, date_like: pd.Timestamp, fallback: float = 0.0) -> float:
        facts = self._instant_facts(df)
        if facts.empty:
            return fallback
        series = pd.Series(facts["val"].to_numpy(dtype=float), index=pd.to_datetime(facts["end"])).sort_index()
        d = series[series.index <= pd.Timestamp(date_like)]
        if d.empty:
            return fallback
        selected_end = pd.Timestamp(d.index[-1])
        self._mark_rows_used(
            facts[facts["end"] == selected_end].tail(1),
            "SharesOutstanding:historical-valuation",
        )
        return float(d.iloc[-1]) / 1e9


# ==============================================================================
# 計算函式
# ==============================================================================
def get_robust_shares(
    ticker: str,
    df_shares: pd.DataFrame,
    sec: SECDataDistiller,
    info: dict,
) -> Tuple[float, str]:
    global _VECTOR_CACHE
    current_time = time.time()
    shares_now, shares_1y_ago = sec.get_shares_now_and_1y(df_shares)
    if shares_now > 0:
        drift = 0.02
        if shares_1y_ago > 0:
            drift = (shares_now / shares_1y_ago) - 1.0
            drift = max(-0.10, min(0.20, drift))
        _VECTOR_CACHE[ticker] = {"shares": shares_now, "drift": drift, "timestamp": current_time}
        return shares_now, "SEC"
    if ticker in _VECTOR_CACHE:
        c = _VECTOR_CACHE[ticker]
        days = (current_time - float(c.get("timestamp", current_time))) / 86400
        return (
            float(c.get("shares", 0.0)) * (1 + float(c.get("drift", 0.02)) * days / 365),
            "CACHE_EXTRAPOLATION",
        )
    yf_shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    if yf_shares and yf_shares > 0:
        return float(yf_shares) / 1e9, "YAHOO_FINANCE"
    return 0.0, "MISSING"




def fetch_price_metrics(ticker: str) -> Optional[dict]:
    close = get_cached_series(ticker, "Close")
    volume = get_cached_series(ticker, "Volume")
    if close is None or volume is None or len(close) < 200:
        return None
    dollar_volume = float((close * volume).tail(30).mean())
    last_close = float(close.iloc[-1])
    high_52w = float(close.tail(252).max())
    m = close.resample("ME").last().dropna()
    momentum = None
    if len(m) >= 13:
        momentum = (float(m.iloc[-2]) / float(m.iloc[-13]) - 1) * 100
    return {
        "last_close": last_close,
        "dollar_volume": dollar_volume,
        "pct_from_52w_high": (last_close / high_52w - 1) * 100 if high_52w > 0 else np.nan,
        "momentum_12m": momentum,
    }




def safe_div(n: float, d: float, default: float = np.nan) -> float:
    try:
        if d == 0 or not math.isfinite(d):
            return default
        return n / d
    except Exception:
        return default


def first_finite_positive(*values: Any) -> float:
    """Return the first explicit positive number without fabricating zero."""
    for value in values:
        number = finite_number(value)
        if number is not None and number > 0:
            return float(number)
    return np.nan



def annual_values_by_year(
    sec: SECDataDistiller,
    df: pd.DataFrame,
    normalized_metric: str = "",
) -> Dict[int, float]:
    """Return one latest-restated annual XBRL value per period-end year."""
    annual = sec._annual_facts(df)
    if annual.empty:
        return {}
    sec._mark_rows_used(annual, f"{normalized_metric}:annual-history")
    values: Dict[int, float] = {}
    for _, row in annual.sort_values(["end", "filed"] if "filed" in annual.columns else ["end"]).iterrows():
        try:
            # SEC ``fy`` identifies the filing's fiscal year and can also appear
            # on comparative prior-period facts. The fact's period end is the
            # stable key for historical calculations.
            year = int(pd.Timestamp(row["end"]).year)
            value = float(row["val"])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            values[year] = value
    return values


def _recent_average(values: Dict[int, float], year: int, window: int = 3) -> float:
    recent = [abs(float(values[y])) for y in sorted(values) if y <= year and math.isfinite(float(values[y]))][-window:]
    return float(np.mean(recent)) if recent else np.nan


def _revenue_growth_for_year(revenue: Dict[int, float], year: int) -> float:
    current = float(revenue.get(year, np.nan))
    previous_years = [y for y in sorted(revenue) if y < year and revenue.get(y, 0) > 0]
    if not previous_years or not math.isfinite(current) or current <= 0:
        return np.nan
    previous = float(revenue[previous_years[-1]])
    return safe_div(current, previous) - 1.0


def _trailing_consecutive_years(years: Iterable[int], limit: int) -> List[int]:
    """Return the latest consecutive fiscal-year suffix, capped at ``limit``."""
    descending = sorted({int(year) for year in years}, reverse=True)
    if not descending or limit <= 0:
        return []
    selected = [descending[0]]
    expected = descending[0] - 1
    for year in descending[1:]:
        if len(selected) >= limit:
            break
        if year == expected:
            selected.append(year)
            expected -= 1
        elif year < expected:
            break
    return sorted(selected)


def minimum_positive_fcf_years(years_available: float) -> int:
    """Require a positive maintenance-FCF majority once three years exist."""
    years = max(0, int(float(years_available)))
    return int(math.ceil(years * 0.60)) if years >= 3 else 0


def _maintenance_excess_ratio(revenue_growth: float) -> float:
    if not math.isfinite(revenue_growth):
        return 0.55
    if revenue_growth <= 0.0:
        return 0.80
    if revenue_growth >= 0.15:
        return 0.20
    if revenue_growth >= 0.05:
        return 0.35
    return 0.55


def estimate_maintenance_capex_amount(capex: float, dna_anchor: float, revenue_growth: float) -> float:
    capex = abs(float(capex)) if math.isfinite(float(capex)) else 0.0
    dna_anchor = abs(float(dna_anchor)) if math.isfinite(float(dna_anchor)) else 0.0
    if capex <= 0:
        return 0.0
    if dna_anchor <= 0:
        return capex
    base = min(capex, dna_anchor)
    excess = max(0.0, capex - base)
    return min(capex, base + excess * _maintenance_excess_ratio(revenue_growth))


def estimate_maintenance_capex_profile(
    sec: SECDataDistiller,
    df_capex: pd.DataFrame,
    df_dna: pd.DataFrame,
    df_rev: pd.DataFrame,
    capex_ttm: float,
    dna_ttm: float,
) -> Dict[str, float | str | bool]:
    capex = abs(float(capex_ttm)) if math.isfinite(capex_ttm) else 0.0
    dna = annual_values_by_year(sec, df_dna, "DnA")
    revenue = annual_values_by_year(sec, df_rev, "Revenue")
    latest_year = max(revenue) if revenue else None
    dna_anchor = abs(float(dna_ttm)) if math.isfinite(dna_ttm) else 0.0
    if latest_year is not None:
        recent_dna = _recent_average(dna, latest_year)
        if math.isfinite(recent_dna):
            dna_anchor = max(dna_anchor, recent_dna / 1e9)

    revenue_growth = np.nan
    rev_q = sec.quarterly_series(df_rev, "Revenue")
    if len(rev_q) >= 8:
        rev_now = float(rev_q.tail(4).sum())
        rev_prev = float(rev_q.iloc[-8:-4].sum())
        revenue_growth = safe_div(rev_now, rev_prev) - 1.0 if rev_prev > 0 else np.nan
    elif latest_year is not None:
        revenue_growth = _revenue_growth_for_year(revenue, latest_year)

    maintenance = estimate_maintenance_capex_amount(capex, dna_anchor, revenue_growth)
    base = min(capex, dna_anchor) if dna_anchor > 0 else capex
    excess = max(0.0, capex - base)
    excess_ratio = _maintenance_excess_ratio(revenue_growth)
    maintenance_low = min(capex, base + excess * max(0.0, excess_ratio - 0.15))
    maintenance_high = min(capex, base + excess * min(1.0, excess_ratio + 0.15))
    capex_to_dna = safe_div(capex, dna_anchor) if dna_anchor > 0 else np.nan
    growth_capex = max(0.0, capex - maintenance)
    complete_dna_years = sum(math.isfinite(float(value)) for value in dna.values())
    if dna_anchor <= 0:
        confidence = "LOW"
    elif complete_dna_years >= 3 and math.isfinite(revenue_growth):
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"
    growth_capex_trap = bool(
        math.isfinite(revenue_growth)
        and revenue_growth <= 0.0
        and math.isfinite(capex_to_dna)
        and capex_to_dna >= 1.5
    )
    return {
        "maintenance_capex_b": maintenance,
        "maintenance_capex_low_b": maintenance_low,
        "maintenance_capex_high_b": maintenance_high,
        "growth_capex_b": growth_capex,
        "dna_anchor_b": dna_anchor,
        "revenue_growth_pct": revenue_growth * 100 if math.isfinite(revenue_growth) else np.nan,
        "capex_to_dna": capex_to_dna,
        "growth_capex_trap": growth_capex_trap,
        "confidence": confidence,
        "method": (
            "D&A anchored maintenance CapEx; excess CapEx classified by trailing revenue growth"
            if dna_anchor > 0
            else "No D&A anchor; all CapEx treated as maintenance"
        ),
    }


def calculate_fcf_stability(
    sec: SECDataDistiller,
    df_ocf: pd.DataFrame,
    df_capex: pd.DataFrame,
    df_sbc: pd.DataFrame,
    df_dna: pd.DataFrame,
    df_rev: pd.DataFrame,
    df_net_income: pd.DataFrame,
) -> Dict[str, float]:
    """Calculate 3Y OCF and up to 5Y maintenance-FCF stability without extra requests."""
    ocf = annual_values_by_year(sec, df_ocf, "OCF")
    capex = annual_values_by_year(sec, df_capex, "CapEx")
    sbc = annual_values_by_year(sec, df_sbc, "SBC")
    dna = annual_values_by_year(sec, df_dna, "DnA")
    revenue = annual_values_by_year(sec, df_rev, "Revenue")
    net_income = annual_values_by_year(sec, df_net_income, "NetIncome")
    # A missing SBC fact is unknown, not zero. Restrict maintenance-FCF history
    # to consecutive years with a complete OCF/CapEx/SBC/revenue/income chain.
    years = _trailing_consecutive_years(
        set(ocf) & set(capex) & set(sbc) & set(revenue) & set(net_income),
        5,
    )
    real_fcf_values: List[float] = []
    margins: List[float] = []
    ocf_values: List[float] = []
    ni_values: List[float] = []
    for year in years:
        dna_anchor = _recent_average(dna, year)
        maintenance_capex = estimate_maintenance_capex_amount(
            abs(capex[year]),
            dna_anchor,
            _revenue_growth_for_year(revenue, year),
        )
        real_fcf = ocf[year] - maintenance_capex - abs(sbc[year])
        real_fcf_values.append(real_fcf)
        ocf_values.append(ocf[year])
        ni_values.append(net_income[year])
        if revenue[year] > 0:
            margins.append(real_fcf / revenue[year])

    latest_three = _trailing_consecutive_years(ocf, 3)
    ocf_3y = sum(ocf[year] for year in latest_three) if latest_three else np.nan
    total_ni = sum(ni_values)
    return {
        "years_available": float(len(years)),
        "positive_years": float(sum(1 for value in real_fcf_values if value > 0)),
        "margin_std_pct": float(np.std(margins) * 100) if len(margins) >= 2 else np.nan,
        "ocf_to_net_income": safe_div(sum(ocf_values), total_ni) if total_ni > 0 else np.nan,
        "real_fcf_to_net_income": safe_div(sum(real_fcf_values), total_ni) if total_ni > 0 else np.nan,
        "ocf_3y_cumulative_b": ocf_3y / 1e9 if math.isfinite(ocf_3y) else np.nan,
        "ocf_3y_years": float(len(latest_three)),
    }


def calculate_per_share_growth_3y(
    sec: SECDataDistiller,
    df_ocf: pd.DataFrame,
    df_capex: pd.DataFrame,
    df_sbc: pd.DataFrame,
    df_dna: pd.DataFrame,
    df_rev: pd.DataFrame,
    df_net_income: pd.DataFrame,
    df_shares: pd.DataFrame,
) -> Dict[str, float]:
    """Calculate split-adjusted annual per-share growth without filling gaps."""
    ocf = annual_values_by_year(sec, df_ocf, "OCF")
    capex = annual_values_by_year(sec, df_capex, "CapEx")
    sbc = annual_values_by_year(sec, df_sbc, "SBC")
    dna = annual_values_by_year(sec, df_dna, "DnA")
    revenue = annual_values_by_year(sec, df_rev, "Revenue")
    net_income = annual_values_by_year(sec, df_net_income, "NetIncome")
    complete_years = sorted(set(ocf) & set(capex) & set(sbc) & set(revenue) & set(net_income))
    if not complete_years:
        return {"fcf_cagr_pct": np.nan, "eps_cagr_pct": np.nan, "years": 0.0}
    latest_year = complete_years[-1]
    base_year = latest_year - 3
    if base_year not in complete_years:
        return {"fcf_cagr_pct": np.nan, "eps_cagr_pct": np.nan, "years": 0.0}

    share_facts = sec._instant_facts(df_shares)
    if share_facts.empty or "end" not in share_facts.columns:
        return {"fcf_cagr_pct": np.nan, "eps_cagr_pct": np.nan, "years": 0.0}

    def shares_for_year(year: int) -> float:
        candidates = share_facts[pd.to_datetime(share_facts["end"]).dt.year == year]
        if candidates.empty:
            return np.nan
        selected = candidates.sort_values("end").iloc[-1]
        adjusted, _ = split_adjusted_share_value(
            sec.ticker,
            float(selected["val"]),
            selected.get("end"),
            selected.get("available_to_model_at", selected.get("filed")),
            str(selected.get("concept") or ""),
            sec.decision_timestamp,
        )
        sec._mark_rows_used(selected, "SharesOutstanding:per-share-growth")
        return adjusted / 1e9

    def annual_real_fcf(year: int) -> float:
        maintenance = estimate_maintenance_capex_amount(
            abs(capex[year]),
            _recent_average(dna, year),
            _revenue_growth_for_year(revenue, year),
        )
        return (ocf[year] - maintenance - abs(sbc[year])) / 1e9

    latest_shares = shares_for_year(latest_year)
    base_shares = shares_for_year(base_year)
    if not all(math.isfinite(value) and value > 0 for value in (latest_shares, base_shares)):
        return {"fcf_cagr_pct": np.nan, "eps_cagr_pct": np.nan, "years": 0.0}
    latest_fcf_ps = safe_div(annual_real_fcf(latest_year), latest_shares)
    base_fcf_ps = safe_div(annual_real_fcf(base_year), base_shares)
    latest_eps = safe_div(net_income[latest_year] / 1e9, latest_shares)
    base_eps = safe_div(net_income[base_year] / 1e9, base_shares)

    def cagr(latest: float, base: float) -> float:
        if not all(math.isfinite(value) and value > 0 for value in (latest, base)):
            return np.nan
        return ((latest / base) ** (1.0 / 3.0) - 1.0) * 100.0

    return {
        "fcf_cagr_pct": cagr(latest_fcf_ps, base_fcf_ps),
        "eps_cagr_pct": cagr(latest_eps, base_eps),
        "years": 3.0,
    }


def calculate_capital_allocation_score(
    real_buyback_b: float,
    issuance_b: float,
    share_change_1y_pct: float,
    share_change_3y_pct: float,
    market_cap_b: float = np.nan,
) -> float:
    """Reward net buyback yield only when it actually reduces share count."""
    score = 60.0
    net_buyback_yield = safe_div(real_buyback_b, market_cap_b) * 100 if market_cap_b > 0 else np.nan
    if math.isfinite(share_change_3y_pct):
        if share_change_3y_pct <= -3.0:
            score += 15.0
        elif share_change_3y_pct > 3.0:
            score -= 35.0
    if real_buyback_b > 0 and math.isfinite(share_change_1y_pct):
        if share_change_1y_pct < 0:
            score += 15.0
            if math.isfinite(net_buyback_yield) and net_buyback_yield >= 1.0:
                score += 10.0
        else:
            score -= 35.0
    if issuance_b > max(real_buyback_b, 0.0) and math.isfinite(share_change_1y_pct) and share_change_1y_pct > 0:
        score -= 10.0
    return round(max(0.0, min(100.0, score)), 2)




def percentile_rank(history: Iterable[float], current: float) -> float:
    vals = [float(x) for x in history if x is not None and math.isfinite(float(x)) and float(x) > 0]
    if not vals or not math.isfinite(current):
        return np.nan
    return float((np.sum(np.array(vals) <= current) / len(vals)) * 100)




def low_percentile(history: Iterable[float], pct: float = LOW_VALUATION_PERCENTILE) -> float:
    vals = [float(x) for x in history if x is not None and math.isfinite(float(x)) and float(x) > 0]
    if not vals:
        return np.nan
    return float(np.nanpercentile(vals, pct))




def classify_three_quarter_trend(rev_q: pd.Series, gp_q: pd.Series) -> Tuple[str, dict]:
    common = pd.concat([rev_q.rename("revenue"), gp_q.rename("gross_profit")], axis=1, join="inner").dropna()
    common = common[common["revenue"] > 0].tail(3)
    if len(common) < 3:
        return "資料不足：無法完成最新三季毛利診斷", {}
    common["gm"] = common["gross_profit"] / common["revenue"]
    rev_change = float(common["revenue"].iloc[-1] / common["revenue"].iloc[0] - 1)
    gm_change_pp = float((common["gm"].iloc[-1] - common["gm"].iloc[0]) * 100)
    gm_monotonic_down = bool(common["gm"].iloc[2] < common["gm"].iloc[1] < common["gm"].iloc[0])
    if rev_change < -0.02 and abs(gm_change_pp) <= 1.5:
        label = "暫時落難好股：營收下滑但毛利持穩，疑似仍有定價權"
    elif rev_change >= -0.02 and (gm_monotonic_down or gm_change_pp <= -2.0):
        label = "結構性價值陷阱：營收未崩但毛利連續失血"
    elif rev_change < -0.02 and gm_change_pp < -2.0:
        label = "雙重惡化：營收與毛利同步失血"
    else:
        label = "中性：三季趨勢未給出明確逆風訊號"
    metrics = {
        "rev_3q_change_pct": rev_change * 100,
        "gm_latest_pct": float(common["gm"].iloc[-1] * 100),
        "gm_3q_change_pp": gm_change_pp,
    }
    return label, metrics




def calc_dsi_series(
    inv_df: pd.DataFrame,
    cogs_q: pd.Series,
    sec: Optional[SECDataDistiller] = None,
) -> pd.Series:
    if inv_df.empty or cogs_q.empty or len(cogs_q) < 4:
        return pd.Series(dtype=float)
    inventory_facts = SECDataDistiller._instant_facts(inv_df)
    if inventory_facts.empty:
        return pd.Series(dtype=float)
    inventory_facts = inventory_facts.sort_values("end").reset_index(drop=True)
    out = {}
    selected_rows = []
    for end in cogs_q.index[-8:]:
        current_candidates = inventory_facts[
            pd.to_datetime(inventory_facts["end"], errors="coerce") <= end
        ]
        if current_candidates.empty:
            continue
        current_row = current_candidates.iloc[-1]
        current_end = pd.Timestamp(current_row["end"])
        age_days = (
            current_end
            - pd.to_datetime(inventory_facts["end"], errors="coerce")
        ).dt.days
        prior_candidates = inventory_facts[
            age_days.between(300, 450, inclusive="both")
        ]
        if prior_candidates.empty:
            continue
        prior_row = prior_candidates.iloc[-1]
        last4 = cogs_q[cogs_q.index <= end].tail(4)
        if len(last4) < 4 or last4.sum() <= 0:
            continue
        average_inventory = (float(prior_row["val"]) + float(current_row["val"])) / 2.0
        out[pd.Timestamp(end)] = average_inventory / float(last4.sum()) * 365
        selected_rows.extend([prior_row, current_row])
    if sec is not None and selected_rows:
        selected = pd.DataFrame(selected_rows)
        dedup_columns = [
            column
            for column in ["evidence_id", "end", "concept"]
            if column in selected.columns
        ]
        if dedup_columns:
            selected = selected.drop_duplicates(subset=dedup_columns)
        sec._mark_rows_used(selected, "Inventory:DSI-average-balance")
    return pd.Series(out).sort_index()


def analyze_dsi_signal(dsi: pd.Series) -> Dict[str, float | str | bool]:
    clean = pd.to_numeric(dsi, errors="coerce").dropna().sort_index()
    if clean.empty:
        return {
            "latest": np.nan,
            "qoq_change_pct": np.nan,
            "yoy_change_pct": np.nan,
            "sequential_down": False,
            "inflection": False,
            "deterioration": False,
            "signal": "資料不足或不適用",
            "score": 50.0,
        }
    latest = float(clean.iloc[-1])
    qoq_change = safe_div(latest, float(clean.iloc[-2])) - 1.0 if len(clean) >= 2 else np.nan
    yoy_change = safe_div(latest, float(clean.iloc[-5])) - 1.0 if len(clean) >= 5 else np.nan
    sequential_down = bool(len(clean) >= 3 and clean.iloc[-1] < clean.iloc[-2] < clean.iloc[-3])
    sequential_up = bool(len(clean) >= 3 and clean.iloc[-1] > clean.iloc[-2] > clean.iloc[-3])
    seasonally_confirmed_down = math.isfinite(yoy_change) and yoy_change <= -0.05
    seasonally_confirmed_up = math.isfinite(yoy_change) and yoy_change >= 0.05
    inflection = bool(sequential_down and seasonally_confirmed_down)
    deterioration = bool(sequential_up and seasonally_confirmed_up)
    if inflection:
        signal, score = "去庫存改善（連兩季下降且季節性確認）", 80.0
    elif deterioration:
        signal, score = "庫存惡化（連兩季上升且季節性確認）", 20.0
    elif sequential_down:
        signal, score = "連兩季下降，但尚未通過年對年季節性確認", 60.0
    else:
        signal, score = "中性", 50.0
    return {
        "latest": latest,
        "qoq_change_pct": qoq_change * 100 if math.isfinite(qoq_change) else np.nan,
        "yoy_change_pct": yoy_change * 100 if math.isfinite(yoy_change) else np.nan,
        "sequential_down": sequential_down,
        "inflection": inflection,
        "deterioration": deterioration,
        "signal": signal,
        "score": score,
    }




def get_upcoming_earnings(
    ticker: str,
    info: Optional[Mapping[str, Any]] = None,
    now: Optional[pd.Timestamp] = None,
) -> List[str]:
    events = []
    now = (
        pd.Timestamp.now(tz="UTC").tz_convert(None)
        if now is None
        else pd.to_datetime(now, utc=True).tz_convert(None)
    )
    end = now + pd.Timedelta(days=30)
    cached_schedule_found = False
    for key in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
        raw = (info or {}).get(key)
        if raw in (None, ""):
            continue
        cached_schedule_found = True
        dt = (
            pd.to_datetime(raw, unit="s", utc=True, errors="coerce")
            if isinstance(raw, (int, float))
            else pd.to_datetime(raw, utc=True, errors="coerce")
        )
        if pd.notna(dt):
            dt = pd.Timestamp(dt).tz_convert(None)
            if now <= dt <= end:
                events.append(f"Earnings: {dt.date()}")
    if cached_schedule_found:
        return sorted(set(events))
    try:
        cal = yf.Ticker(ticker).calendar
        if isinstance(cal, dict):
            candidates = cal.get("Earnings Date") or cal.get("EarningsDate") or []
            if not isinstance(candidates, list):
                candidates = [candidates]
            for c in candidates:
                dt = pd.to_datetime(c, errors="coerce")
                if pd.notna(dt):
                    dt = dt.tz_localize(None) if getattr(dt, "tzinfo", None) else dt
                    if now <= dt <= end:
                        events.append(f"Earnings: {dt.date()}")
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
            for idx, row in cal.iterrows():
                for item in row.values:
                    dt = pd.to_datetime(item, errors="coerce")
                    if pd.notna(dt):
                        dt = dt.tz_localize(None) if getattr(dt, "tzinfo", None) else dt
                        if now <= dt <= end:
                            events.append(f"Earnings: {dt.date()}")
    except Exception:
        pass
    return sorted(set(events))


def short_interest_data_age_days(info: dict, now: Optional[pd.Timestamp] = None) -> float:
    raw = info.get("dateShortInterest") or info.get("shortInterestDate")
    if raw in (None, ""):
        return np.nan
    try:
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            unit = "ms" if float(raw) > 10_000_000_000 else "s"
            observed = pd.to_datetime(raw, unit=unit, utc=True)
        else:
            observed = pd.to_datetime(raw, utc=True, errors="coerce")
        if pd.isna(observed):
            return np.nan
        reference = now if now is not None else pd.Timestamp.now(tz="UTC")
        if reference.tzinfo is None:
            reference = reference.tz_localize("UTC")
        age = (reference - observed).total_seconds() / 86400.0
        return float(age) if age >= -2.0 else np.nan
    except Exception:
        return np.nan




def historical_valuation(
    ticker: str,
    sec: SECDataDistiller,
    df_ebit: pd.DataFrame,
    df_dna: pd.DataFrame,
    df_debt: pd.DataFrame,
    df_debt_current: pd.DataFrame,
    df_cash: pd.DataFrame,
    df_net_income: pd.DataFrame,
    df_shares: pd.DataFrame,
    current_ev_ebitda: float,
    current_pe: float,
    shares_now: float,
    debt_component_frames: Optional[Mapping[str, pd.DataFrame]] = None,
) -> dict:
    """Build comparable historical multiples from raw prices and filed annual data."""
    empty_result = {
        "ev_ebitda_hist": [],
        "pe_hist": [],
        "ev_evidence_ids": [],
        "pe_evidence_ids": [],
        "ev_ebitda_percentile": np.nan,
        "pe_percentile": np.nan,
        "ev_ebitda_floor": np.nan,
        "pe_floor": np.nan,
        "valid_years": 0,
        "total_years": 0,
        "coverage": 0.0,
        "coverage_status": "MISSING",
    }
    # Use the first filing for each historical period so later restatements do
    # not introduce look-ahead into the price/multiple history.
    ebit_a = sec._annual_facts(df_ebit, latest_filed=False)
    if ebit_a.empty or "end" not in ebit_a.columns:
        return empty_result

    dna_a = sec._annual_facts(df_dna, latest_filed=False)
    ni_a = sec._annual_facts(df_net_income, latest_filed=False)
    debt_inputs: Dict[str, pd.DataFrame] = {
        "DebtTotal": df_debt,
        "DebtCurrent": df_debt_current,
    }
    if debt_component_frames:
        debt_inputs.update(debt_component_frames)
    debt_history = {
        name: sec._annual_facts(frame, latest_filed=False)
        for name, frame in debt_inputs.items()
    }
    cash_a = sec._annual_facts(df_cash, latest_filed=False)
    shares_facts = sec._instant_facts(df_shares, latest_filed=False)
    long_term_only = {
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebtNoncurrent",
    }

    candidate_rows = ebit_a.tail(10)
    total_years = len(candidate_rows)
    rows = []
    for _, erow in candidate_rows.iterrows():
        period_end = pd.Timestamp(erow["end"])
        def latest_asof_row(
            frame: pd.DataFrame,
            max_age_days: Optional[int] = None,
        ) -> Optional[pd.Series]:
            if frame.empty or "end" not in frame.columns:
                return None
            sub = frame[frame["end"] <= period_end]
            if sub.empty:
                return None
            row = sub.sort_values("end").iloc[-1]
            if (
                max_age_days is not None
                and (period_end - pd.Timestamp(row["end"])).days > max_age_days
            ):
                return None
            return row

        def exact_period_row(frame: pd.DataFrame) -> Optional[pd.Series]:
            if frame.empty or "end" not in frame.columns:
                return None
            matched = frame[frame["end"] == period_end]
            return matched.iloc[-1] if not matched.empty else None

        debt_rows = {
            name: latest_asof_row(frame, max_age_days=45)
            for name, frame in debt_history.items()
        }
        debt_row = debt_rows.get("DebtTotal")
        debt_current_row = debt_rows.get("DebtCurrent")
        cash_row = latest_asof_row(cash_a, max_age_days=45)
        dna_row = exact_period_row(dna_a)
        ni_row = exact_period_row(ni_a)
        shares_row = latest_asof_row(shares_facts, max_age_days=45)
        if debt_row is None or dna_row is None or cash_row is None or shares_row is None:
            continue

        def debt_value(name: str) -> float:
            row = debt_rows.get(name)
            return float(row["val"]) / 1e9 if row is not None else 0.0

        def debt_concept(name: str) -> str:
            row = debt_rows.get(name)
            return str(row.get("concept") or "") if row is not None else ""

        total_concept = debt_concept("DebtTotal")
        current_concept = debt_concept("DebtCurrent")
        short_total_concept = debt_concept("DebtShortTermTotal")
        debt = _compose_total_debt(
            debt_value("DebtTotal"),
            total_concept,
            debt_value("DebtCurrent"),
            current_concept,
            debt_value("DebtShortTermTotal"),
            short_total_concept,
            debt_value("DebtOtherShortTerm"),
            debt_value("DebtCommercialPaper"),
            debt_value("DebtFinanceLease"),
        )
        if not math.isfinite(debt):
            continue

        debt_source_names = ["DebtTotal"]
        if total_concept not in CONSOLIDATED_DEBT_TAGS:
            if total_concept in long_term_only and current_concept:
                debt_source_names.append("DebtCurrent")
            if current_concept not in {"DebtCurrent", "ShortTermBorrowings"}:
                if short_total_concept:
                    debt_source_names.append("DebtShortTermTotal")
                else:
                    if debt_concept("DebtOtherShortTerm"):
                        debt_source_names.append("DebtOtherShortTerm")
                    if debt_concept("DebtCommercialPaper"):
                        debt_source_names.append("DebtCommercialPaper")
        if (
            debt_concept("DebtFinanceLease")
            and total_concept not in DEBT_TAGS_INCLUDING_FINANCE_LEASE
            and current_concept not in DEBT_CURRENT_TAGS_INCLUDING_FINANCE_LEASE
        ):
            debt_source_names.append("DebtFinanceLease")
        selected_debt_rows = [
            debt_rows[name]
            for name in dict.fromkeys(debt_source_names)
            if debt_rows.get(name) is not None
        ]
        cash = float(cash_row["val"]) / 1e9
        dna = float(dna_row["val"]) / 1e9
        ni = float(ni_row["val"]) / 1e9 if ni_row is not None else np.nan
        ebit = float(erow["val"]) / 1e9
        ebitda = ebit + abs(dna)
        if ebitda <= 0:
            continue

        source_rows = [erow, dna_row, cash_row, shares_row, *selected_debt_rows]
        if ni_row is not None:
            source_rows.append(ni_row)
        availability_times = []
        for source_row in source_rows:
            available_at = source_row.get("available_to_model_at")
            filed = source_row.get("filed")
            candidate_time = available_at if pd.notna(available_at) else filed
            parsed_time = sec._utc_naive_timestamp(candidate_time)
            if pd.notna(parsed_time):
                availability_times.append(pd.Timestamp(parsed_time))
        evidence_date = max(availability_times) if availability_times else period_end
        # The historical multiple is reconstructed only after every selected
        # filing input was available, plus one full-day information lag.
        valuation_date = evidence_date.normalize() + pd.Timedelta(days=1)
        # yf.download(auto_adjust=False) returns the raw close on the valuation
        # date. Align the period share count only through that date; applying a
        # later split would multiply historical market capitalization.
        adjusted_shares, split_factor = split_adjusted_share_value(
            ticker,
            float(shares_row["val"]),
            shares_row.get("end"),
            shares_row.get("available_to_model_at", shares_row.get("filed")),
            str(shares_row.get("concept") or ""),
            valuation_date,
        )
        shares = adjusted_shares / 1e9
        if shares <= 0:
            continue
        price = get_price_on_or_after(ticker, valuation_date)
        if not math.isfinite(price) or price <= 0:
            continue
        period_key = str(period_end.date())
        price_evidence_id = sec.record_observed_input(
            f"Historical_MarketPrice_{period_key}",
            price,
            "USD_per_share",
            "YAHOO_FINANCE",
            "first close after all selected filing inputs plus one-day lag",
            period_end=valuation_date,
        )
        share_evidence_id = str(shares_row.get("evidence_id") or "")
        split_evidence_id = ""
        if not math.isclose(split_factor, 1.0):
            split_evidence_id = sec.record_observed_input(
                f"Historical_Share_Split_Factor_{period_key}",
                split_factor,
                "ratio",
                "YAHOO_FINANCE",
                "Stock Splits",
                period_end=valuation_date,
            )
        market_cap_source_ids = [price_evidence_id, share_evidence_id]
        if split_evidence_id:
            market_cap_source_ids.append(split_evidence_id)
        historical_mcap_id = sec._record_derived_from_ids(
            f"Historical_MarketCap_{period_key}",
            price * shares,
            "USD_B",
            "historical market price * split-adjusted period shares outstanding",
            market_cap_source_ids,
            "HistoricalValuation:model-input",
        )
        historical_ebitda_id = sec._record_derived_from_ids(
            f"Historical_EBITDA_{period_key}",
            ebitda,
            "USD_B",
            "reported annual EBIT + reported annual D&A",
            [str(erow.get("evidence_id") or ""), str(dna_row.get("evidence_id") or "")],
            "HistoricalValuation:model-input",
        )
        debt_source_ids = [
            str(row.get("evidence_id") or "") for row in selected_debt_rows
        ]
        ev_source_ids = [
            historical_mcap_id,
            historical_ebitda_id,
            *debt_source_ids,
            str(cash_row.get("evidence_id") or ""),
        ]
        mcap = price * shares
        ev_ebitda = safe_div(mcap + debt - cash, ebitda)
        pe = safe_div(mcap, ni)
        ev_evidence_id = sec._record_derived_from_ids(
            f"Historical_EV_EBITDA_{period_key}",
            ev_ebitda,
            "x",
            "(historical MarketCap + Debt - Cash) / historical EBITDA",
            ev_source_ids,
            "HistoricalValuation:model-input",
        )
        pe_evidence_id = ""
        if ni_row is not None and math.isfinite(pe):
            pe_evidence_id = sec._record_derived_from_ids(
                f"Historical_PE_{period_key}",
                pe,
                "x",
                "historical MarketCap / reported annual Net Income",
                [historical_mcap_id, str(ni_row.get("evidence_id") or "")],
                "HistoricalValuation:model-input",
            )
        rows.append(
            {
                "end": period_key,
                "filed": str(valuation_date.date()),
                "EV_EBITDA": ev_ebitda,
                "PE": pe,
                "ev_evidence_id": ev_evidence_id,
                "pe_evidence_id": pe_evidence_id,
            }
        )

    ev_hist = [r["EV_EBITDA"] for r in rows if math.isfinite(r["EV_EBITDA"]) and r["EV_EBITDA"] > 0]
    pe_hist = [r["PE"] for r in rows if math.isfinite(r["PE"]) and r["PE"] > 0]
    min_history = 5
    valid_years = len(ev_hist)
    coverage = valid_years / total_years if total_years else 0.0
    coverage_status = "VALID" if valid_years >= min_history and coverage >= 0.60 else "MISSING"
    return {
        "ev_ebitda_hist": rows,
        "pe_hist": pe_hist,
        "ev_evidence_ids": [r["ev_evidence_id"] for r in rows if r.get("ev_evidence_id")],
        "pe_evidence_ids": [r["pe_evidence_id"] for r in rows if r.get("pe_evidence_id")],
        "ev_ebitda_percentile": percentile_rank(ev_hist, current_ev_ebitda) if len(ev_hist) >= min_history else np.nan,
        "pe_percentile": percentile_rank(pe_hist, current_pe) if len(pe_hist) >= min_history else np.nan,
        "ev_ebitda_floor": low_percentile(ev_hist) if len(ev_hist) >= min_history else np.nan,
        "pe_floor": low_percentile(pe_hist) if len(pe_hist) >= min_history else np.nan,
        "valid_years": valid_years,
        "total_years": total_years,
        "coverage": coverage,
        "coverage_status": coverage_status,
    }

def implied_ebitda_cagr(
    current_ev: float,
    base_ebitda: float,
    target_multiple: float,
    years: int = 3,
    required_return: float = REVERSE_DCF_REQUIRED_RETURN,
) -> float:
    """Solve EBITDA growth required to earn the chosen EV return at exit."""
    if base_ebitda <= 0 or current_ev <= 0 or target_multiple <= 0 or years <= 0:
        return np.nan
    required_return = max(-0.95, float(required_return))
    future_ev = current_ev * ((1.0 + required_return) ** years)
    required = future_ev / target_multiple
    return (required / base_ebitda) ** (1 / years) - 1


def calculate_financial_stress(
    ebit_b: float,
    ebitda_b: float,
    interest_b: float,
    total_debt_b: float,
    cash_b: float,
    real_fcf_b: float,
    tax_rate: float,
    ebitda_drop: float,
) -> Dict[str, float | bool]:
    """Hold D&A fixed and test debt service after an EBITDA shock."""
    drop = max(0.0, min(0.95, float(ebitda_drop)))
    stressed_ebitda = ebitda_b * (1.0 - drop)
    stressed_ebit = ebit_b - (ebitda_b - stressed_ebitda)
    net_debt = max(total_debt_b - cash_b, 0.0)
    if total_debt_b <= 0.01 or net_debt <= 0:
        stressed_icr = math.inf
    else:
        stressed_icr = safe_div(stressed_ebit, interest_b)
    net_debt_to_ebitda = safe_div(net_debt, stressed_ebitda)
    after_tax_earnings_loss = max(0.0, ebitda_b - stressed_ebitda) * (1.0 - tax_rate)
    stressed_real_fcf = real_fcf_b - after_tax_earnings_loss
    liquidity_after_stress = cash_b + min(stressed_real_fcf, 0.0)
    cash_flow_survives = bool(
        stressed_real_fcf >= 0
        or (net_debt <= 0 and liquidity_after_stress > 0)
    )
    survives = bool(
        stressed_ebitda > 0
        and cash_flow_survives
        and (
            net_debt <= 0
            or (math.isfinite(stressed_icr) and stressed_icr >= STRESS_ICR_MIN)
            or math.isinf(stressed_icr)
        )
    )
    return {
        "ebitda_b": stressed_ebitda,
        "ebit_b": stressed_ebit,
        "icr": stressed_icr,
        "net_debt_to_ebitda": net_debt_to_ebitda,
        "real_fcf_b": stressed_real_fcf,
        "liquidity_after_stress_b": liquidity_after_stress,
        "cash_flow_survives": cash_flow_survives,
        "survives": survives,
    }


def calculate_interest_coverage_gate(
    ebit_b: float,
    interest_b: float,
    total_debt_b: float,
    cash_b: float,
) -> Dict[str, float | str | bool]:
    """Do not require an interest proxy when cash fully covers reported debt."""
    if total_debt_b <= 0.01:
        return {"icr": math.inf, "missing_critical": False, "mode": "immaterial_debt"}
    if math.isfinite(cash_b) and cash_b >= total_debt_b:
        return {"icr": math.inf, "missing_critical": False, "mode": "net_cash"}
    if math.isfinite(interest_b) and interest_b > 0:
        return {
            "icr": safe_div(ebit_b, interest_b),
            "missing_critical": False,
            "mode": "reported_interest",
        }
    return {"icr": np.nan, "missing_critical": True, "mode": "missing_interest"}




# ==============================================================================
# 產業特定 Agent 驗證任務產生器
# ==============================================================================
def _info_blob(info: dict) -> str:
    """把 yfinance info 中可用的產業/業務描述合併成可搜尋字串。"""
    parts = [
        str(info.get("sector") or ""),
        str(info.get("industry") or ""),
        str(info.get("longBusinessSummary") or ""),
        str(info.get("quoteType") or ""),
    ]
    return " ".join(parts).lower()




def _has_any(haystack: str, needles: Iterable[str]) -> bool:
    haystack = haystack.lower()
    for needle in needles:
        n = needle.lower().strip()
        if not n:
            continue
        # 短詞如 chip/gpu/hbm/euv 需用字界，避免把 Chipotle、public、services 之類誤判成半導體。
        if len(n) <= 4 and n.replace("-", "").isalnum():
            if re.search(rf"\b{re.escape(n)}\b", haystack):
                return True
        elif n in haystack:
            return True
    return False




def build_agent_verification_plan(
    ticker: str,
    info: dict,
    implied_cagr_pct: float,
    real_fcf_yield_pct: float,
) -> Tuple[str, List[str]]:
    """
    依公司所屬產業動態產生 Physical_Check 與 must_verify。

    修正重點：
    - 只有半導體、AI 硬體、資料中心/CSP 相關標的才要求查 TSMC/ASML/CSP。
    - 非科技股改查自身產業的硬限制：需求、產能、庫存、法規、融資、商品價格等。
    - 高隱含 CAGR 標的一律增加「產業瓶頸可支撐性」壓力測試，但不套錯產業模板。
    """
    t = str(ticker).upper().strip()
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    industry_text = industry.lower()


    csp_tickers = {"MSFT", "AMZN", "GOOG", "GOOGL", "META", "ORCL", "IBM"}
    semi_keywords = [
        "semiconductor", "semiconductors", "semi", "foundry", "fabless", "wafer",
        "lithography", "euv", "duv", "advanced packaging", "chip", "chips",
        "integrated circuit", "memory", "dram", "nand", "hbm",
        "gpu", "accelerator", "ai accelerator", "co-packaged optics",
    ]
    ai_hardware_industries = [
        "computer hardware", "communication equipment", "electronic components",
        "semiconductor equipment", "consumer electronics",
    ]
    software_keywords = [
        "software", "saas", "application", "cybersecurity", "internet content",
        "information technology services", "cloud software", "platform", "subscription",
    ]
    healthcare_keywords = [
        "healthcare", "biotechnology", "biotech", "drug", "pharmaceutical", "medical devices",
        "diagnostics", "clinical", "therapeutics", "managed care", "health information",
    ]
    financial_keywords = [
        "bank", "banks", "insurance", "asset management", "capital markets", "credit",
        "mortgage", "financial", "broker", "fintech", "payments",
    ]
    energy_keywords = [
        "oil", "gas", "lng", "energy", "refining", "exploration", "production",
        "midstream", "coal", "renewable", "solar", "wind", "uranium",
    ]
    consumer_keywords = [
        "retail", "restaurant", "apparel", "consumer", "food", "beverage", "travel",
        "hotel", "casino", "auto", "household", "personal products",
    ]
    industrial_keywords = [
        "industrial", "machinery", "aerospace", "defense", "transport", "railroad",
        "trucking", "logistics", "electrical equipment", "building products",
    ]
    materials_keywords = [
        "materials", "chemical", "steel", "aluminum", "copper", "mining", "paper",
        "packaging", "construction materials", "fertilizer",
    ]
    real_estate_keywords = ["reit", "real estate", "property", "residential", "office", "retail reit"]
    utility_keywords = ["utility", "utilities", "regulated electric", "water utility", "gas utility"]


    tasks: List[str] = [
        f"核對 {t} 最新年度申報（10-K/20-F/40-F）與 10-Q footnotes，確認 EBITDA non-recurring / restructuring / impairment / litigation / tax benefit 等調整項。",
        f"抓取 {t} 未來 30 天財報、法說、投資人日、重大產業會議與公司公告。",
    ]


    is_csp = t in csp_tickers
    is_semi_or_ai_hardware = sector.lower() == "technology" and (
        _has_any(industry_text, semi_keywords)
        or _has_any(industry_text, ai_hardware_industries)
    )


    if is_csp:
        physical_check = (
            "CSP/雲端/資料中心：核對 MSFT/AMZN/GOOGL/META/ORCL 等 CapEx 指引、資料中心供電/併網/機櫃/冷卻、"
            "GPU/HBM 取得能力與折舊壓力；不直接用 TSMC 作為唯一瓶頸。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif is_semi_or_ai_hardware:
        physical_check = (
            "半導體/AI硬體：核對 TSMC 先進製程/CoWoS/先進封裝產能、ASML EUV/DUV backlog 與交期、"
            "HBM/ABF/電力/散熱/伺服器供應鏈，以及 CSP CapEx 是否足以支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif _has_any(industry_text, software_keywords):
        physical_check = (
            "軟體/網路/IT服務：不套用 TSMC/ASML。核對 ARR/RPO、NRR/churn、雲端用量、席次擴張、定價權、"
            "SBC 稀釋與客戶預算週期是否支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "healthcare" or _has_any(industry_text, healthcare_keywords):
        physical_check = (
            "醫療/生技：不套用 TSMC/ASML。核對 FDA/PDUFA/臨床讀出、專利懸崖、reimbursement、藥價壓力、"
            "產能/供應短缺與 payer mix。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "financial services" or _has_any(industry_text, financial_keywords):
        physical_check = (
            "金融：不套用 TSMC/ASML。核對 NIM、存款 beta、信用損失/逾放、資本適足率、流動性、商辦/消費信貸曝險與殖利率曲線。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "energy" or _has_any(industry_text, energy_keywords):
        physical_check = (
            "能源：不套用 TSMC/ASML。核對油氣/LNG/電價曲線、crack spread、儲量/decline rate、hedging、"
            "管線/液化/運輸產能與維持性 CapEx。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() in {"consumer cyclical", "consumer defensive"} or _has_any(industry_text, consumer_keywords):
        physical_check = (
            "消費/零售：不套用 TSMC/ASML。核對 same-store sales、客流/客單、促銷強度、庫存週轉、折扣壓力、"
            "消費信貸與供應鏈交期。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "industrials" or _has_any(
        industry_text,
        [*industrial_keywords, "scientific & technical instruments"],
    ):
        physical_check = (
            "工業：不套用 TSMC/ASML。核對訂單/backlog、book-to-bill、產能利用率、交期、原物料/工資成本、"
            "PMI/終端需求與客戶 CapEx 週期。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "basic materials" or _has_any(industry_text, materials_keywords):
        physical_check = (
            "原物料：不套用 TSMC/ASML。核對商品價格、礦山/冶煉產能、現金成本曲線、庫存、能源成本、"
            "環保/出口限制與下游客戶補庫。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "real estate" or _has_any(industry_text, real_estate_keywords):
        physical_check = (
            "REIT/不動產：不套用 TSMC/ASML。核對 occupancy、leasing spread、租約到期、再融資牆、cap rate、"
            "利率敏感度與資產出售能力。"
        )
        tasks.append(f"【適用】{physical_check}")
    elif sector.lower() == "utilities" or _has_any(industry_text, utility_keywords):
        physical_check = (
            "公用事業：不套用 TSMC/ASML。核對核准 ROE、rate base 成長、燃料成本轉嫁、電網/發電 CapEx、"
            "利率與監管案件時程。"
        )
        tasks.append(f"【適用】{physical_check}")
    else:
        physical_check = (
            "通用產業瓶頸：非半導體/AI/CSP，不套用 TSMC/ASML/CSP 模板。核對本業需求、產能、訂單、庫存、價格、"
            "融資與法規限制是否支撐隱含 EBITDA CAGR。"
        )
        tasks.append(f"【適用】{physical_check}")


    if math.isfinite(implied_cagr_pct):
        if implied_cagr_pct > 25.0:
            tasks.append(
                f"高隱含成長壓力測試：{t} Implied EBITDA CAGR 3Y={implied_cagr_pct:.1f}%，必須用上述產業瓶頸逐項驗證；無證據則降級為博弈泡沫。"
            )
        elif implied_cagr_pct < -10.0:
            tasks.append(
                f"反向預期差壓力測試：{t} Implied EBITDA CAGR 3Y={implied_cagr_pct:.1f}%，確認市場是否過度折價或基本面永久受損。"
            )


    if math.isfinite(real_fcf_yield_pct) and real_fcf_yield_pct > 8.0:
        tasks.append(f"高 FCF Yield 防偽：核對 {t} 是否因維持性 CapEx 低估、一次性營運資金流入或裁員/重組造成短期美化。")


    tasks.append(f"抓取官方或付費短倉資料，複核 {t} Short Interest % Float 與 Days to Cover；高軋空僅列為波動風險，不放寬基本面門檻。")
    return physical_check, tasks




SUPPORTED_EQUITY_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX"}


def assess_data_confidence(
    evidence_stats: dict,
    ttm_methods: Dict[str, str],
    *,
    share_change_1y_available: bool,
    share_change_3y_available: bool,
    roic_available: bool,
    valuation_history_available: bool,
    reconciliation_warning: bool,
    sbc_sec_evidence_available: bool,
    current_shares_sec_evidence_available: bool,
    share_basis_discontinuity: bool = False,
    debt_sec_evidence_available: bool = True,
    debt_fully_cash_covered: bool = False,
    tax_rate_sec_evidence_available: bool = True,
    interest_cash_proxy_used: bool = False,
) -> Dict[str, object]:
    score = 100.0
    reasons: List[str] = []
    selected_count = int(evidence_stats.get("selected_source_count") or 0)
    accepted_ratio = float(evidence_stats.get("accepted_at_ratio") or 0.0)
    fallback_tag_ratio = float(evidence_stats.get("fallback_tag_ratio") or 0.0)
    period_anomaly_count = int(evidence_stats.get("period_anomaly_count") or 0)
    if selected_count <= 0:
        score -= 35.0
        reasons.append("No selected SEC evidence could be linked to source filings")
    elif accepted_ratio < 0.50:
        score -= 35.0
        reasons.append("More than half of selected facts use conservative filed-date availability fallback")
    elif accepted_ratio < 0.80:
        score -= 15.0
        reasons.append("Some selected facts lack EDGAR acceptance timestamps")
    if period_anomaly_count:
        score -= min(30.0, period_anomaly_count * 10.0)
        reasons.append(f"{period_anomaly_count} selected facts have abnormal reporting periods")
    if fallback_tag_ratio >= 0.75:
        score -= 10.0
        reasons.append("Most selected facts rely on lower-priority XBRL tag mappings")
    elif fallback_tag_ratio >= 0.40:
        score -= 5.0
        reasons.append("A material share of selected facts relies on alternate XBRL tags")

    methods = list(ttm_methods.values())
    annual_fallbacks = sum("fallback annual" in method.lower() for method in methods)
    quarter_fallbacks = sum("fallback sum" in method.lower() for method in methods)
    if annual_fallbacks:
        score -= min(30.0, annual_fallbacks * 10.0)
        reasons.append(f"{annual_fallbacks} critical TTM metrics fell back to annual values")
    if quarter_fallbacks:
        score -= min(15.0, quarter_fallbacks * 3.0)
        reasons.append(f"{quarter_fallbacks} critical TTM metrics use derived-quarter fallback")
    if not share_change_1y_available:
        score -= 5.0
        reasons.append("One-year share-count evidence is unavailable")
    if not share_change_3y_available:
        score -= 8.0
        reasons.append("Three-year dilution evidence is unavailable")
    if not roic_available:
        score -= 35.0
        reasons.append("ROIC cannot be verified from reported invested capital")
    if not valuation_history_available:
        score -= 35.0
        reasons.append("Comparable point-in-time valuation history is unavailable")
    if reconciliation_warning:
        score -= 10.0
        reasons.append("EBITDA source reconciliation exceeds tolerance")
    if not sbc_sec_evidence_available:
        score -= 10.0
        reasons.append("SBC uses a non-SEC fallback or is unavailable")
    if not current_shares_sec_evidence_available:
        score -= 10.0
        reasons.append("Current shares outstanding use a cache or market-data fallback")
    if not debt_sec_evidence_available:
        penalty = 10.0 if debt_fully_cash_covered else 20.0
        score -= penalty
        reasons.append(
            "Total debt uses a non-SEC fallback; cash fully covers debt"
            if debt_fully_cash_covered
            else "Total debt uses a non-SEC current-market-data fallback"
        )
    if not tax_rate_sec_evidence_available:
        score -= 5.0
        reasons.append("Effective tax rate uses a conservative model assumption")
    if interest_cash_proxy_used:
        score -= 10.0
        reasons.append("ICR uses cash interest paid as a proxy for accrual interest expense")
    if share_basis_discontinuity:
        score -= 40.0
        reasons.append("Share-count history has an unexplained basis discontinuity after split adjustment")
    score = round(max(0.0, min(100.0, score)), 2)
    return {
        "score": score,
        "abstain": score < MIN_DATA_CONFIDENCE,
        "reasons": reasons or ["Source lineage and critical metric coverage are complete"],
    }


def fact_age_limit_days(frame: pd.DataFrame) -> int:
    """Allow the longer window only for genuine 20-F/40-F annual evidence."""
    if frame.empty or "form" not in frame.columns or "end" not in frame.columns:
        return MAX_DOMESTIC_CORE_FACT_AGE_DAYS
    ends = pd.to_datetime(frame["end"], errors="coerce")
    latest_end = ends.max()
    if pd.isna(latest_end):
        return MAX_DOMESTIC_CORE_FACT_AGE_DAYS
    latest_forms = (
        frame.loc[ends == latest_end, "form"].fillna("").astype(str).str.upper()
    )
    if latest_forms.str.startswith(("20-F", "40-F")).any():
        return MAX_FOREIGN_ANNUAL_FACT_AGE_DAYS
    return MAX_DOMESTIC_CORE_FACT_AGE_DAYS


def stale_required_fact_names(
    required_facts: Mapping[str, pd.DataFrame],
    decision_timestamp: Any,
    max_age_days: Optional[int] = None,
) -> List[str]:
    """Identify critical SEC inputs whose latest economic period is too old."""
    decision = pd.to_datetime(decision_timestamp, errors="coerce")
    if pd.isna(decision):
        return sorted(required_facts)
    if getattr(decision, "tzinfo", None) is not None:
        decision = decision.tz_localize(None)
    stale: List[str] = []
    for name, frame in required_facts.items():
        if frame.empty or "end" not in frame.columns:
            continue
        latest_end = pd.to_datetime(frame["end"], errors="coerce").max()
        if pd.isna(latest_end):
            stale.append(name)
            continue
        if getattr(latest_end, "tzinfo", None) is not None:
            latest_end = latest_end.tz_localize(None)
        age_limit = (
            int(max_age_days)
            if max_age_days is not None
            else fact_age_limit_days(frame)
        )
        if (decision.normalize() - latest_end.normalize()).days > age_limit:
            stale.append(name)
    return stale


def common_equity_rejection_reason(ticker: str, info: dict) -> str:
    """Reject funds, OTC listings, preferred/debt instruments, and incomplete metadata."""
    if not re.fullmatch(r"[A-Z]{1,6}(?:[-.][A-Z])?", str(ticker).upper()):
        return "非標準普通股代號"
    quote_type = str(info.get("quoteType") or "").upper()
    if quote_type != "EQUITY":
        return f"非普通股商品 quoteType={quote_type or 'missing'}"
    exchange = str(info.get("exchange") or "").upper()
    if exchange not in SUPPORTED_EQUITY_EXCHANGES:
        return f"非主要美國交易所 exchange={exchange or 'missing'}"
    if info.get("fundFamily") or info.get("category"):
        return "基金或 ETF"
    if not str(info.get("sector") or "").strip() or not str(info.get("industry") or "").strip():
        return "產業分類缺失"
    return ""


def prepare_uploaded_universe(df: pd.DataFrame) -> Dict[str, str]:
    """Validate a manually uploaded universe after yf.info has been prefetched."""
    work = df.copy()
    if "Status" in work.columns:
        work = work[work["Status"].astype(str).str.upper().eq("PASS")]
    accepted: Dict[str, str] = {}
    seen_ciks = set()
    rejected = []
    for _, row in work.iterrows():
        ticker = str(row["Ticker"]).upper().strip()
        cik = str(row["CIK"]).replace(".0", "").zfill(10)
        reason = common_equity_rejection_reason(ticker, safe_yf_info(ticker))
        if not cik.isdigit() or int(cik) <= 0:
            reason = reason or "CIK 無效"
        if cik in seen_ciks:
            reason = reason or "同一 CIK 重複上市商品"
        if reason:
            rejected.append(f"{ticker}: {reason}")
            continue
        accepted[ticker] = cik
        seen_ciks.add(cik)
    if rejected:
        logger.warning("名單驗證排除 %d 檔；範例：%s", len(rejected), "; ".join(rejected[:12]))
    if not accepted:
        raise ValueError("上傳名單經普通股與資料完整性驗證後為空。")
    return accepted


def _bounded_score(value: float, low: float, high: float, missing: float = 0.0) -> float:
    if not math.isfinite(value):
        return missing
    if high <= low:
        return missing
    return float(max(0.0, min(100.0, (value - low) / (high - low) * 100.0)))


def dynamic_implied_cagr_limit(roic_pct: float, gm_change_pp: float) -> float:
    roic_adjustment = (
        float(np.clip((roic_pct - 15.0) * 0.60, -6.0, 10.0))
        if math.isfinite(roic_pct)
        else -3.0
    )
    margin_adjustment = (
        float(np.clip(gm_change_pp * 2.0, -15.0, 6.0))
        if math.isfinite(gm_change_pp)
        else -3.0
    )
    limit = float(np.clip(30.0 + roic_adjustment + margin_adjustment, 15.0, 45.0))
    if math.isfinite(gm_change_pp) and gm_change_pp <= -2.0:
        limit = min(limit, 15.0)
    return round(limit, 2)


def _expectations_score(implied_cagr: float, roic_pct: float = np.nan, gm_change_pp: float = np.nan) -> float:
    if not math.isfinite(implied_cagr):
        return 20.0
    if -10.0 <= implied_cagr <= 15.0:
        return 100.0
    if implied_cagr < -10.0:
        return 35.0
    limit = dynamic_implied_cagr_limit(roic_pct, gm_change_pp)
    if implied_cagr <= limit:
        span = max(limit - 15.0, 1.0)
        return max(55.0, 100.0 - ((implied_cagr - 15.0) / span) * 45.0)
    return max(0.0, 55.0 - ((implied_cagr - limit) / 15.0) * 55.0)


def calculate_long_term_scores(r: "ModeCResult") -> Dict[str, float]:
    valuation_pct = r.EV_EBITDA_10Y_Percentile
    valuation_score = 100.0 - valuation_pct if math.isfinite(valuation_pct) and 0.0 <= valuation_pct <= 100.0 else 20.0
    fcf_score = _bounded_score(r.Real_FCF_Yield_pct, 0.0, 10.0, missing=0.0)
    value_score = valuation_score * 0.55 + fcf_score * 0.45

    if math.isinf(r.ICR) and r.ICR > 0:
        icr_score = 100.0
    else:
        icr_score = _bounded_score(r.ICR, 1.0, 10.0, missing=20.0)
    fcf_quality = 100.0 if r.Real_FCF_Yield_pct >= 5.0 else 70.0 if r.Real_FCF_Yield_pct >= 2.0 else 35.0 if r.Real_FCF_Yield_pct > 0 else 0.0
    if "暫時落難好股" in r.GM_Diagnosis:
        trend_score = 85.0
    elif "中性" in r.GM_Diagnosis:
        trend_score = 70.0
    elif "結構性價值陷阱" in r.GM_Diagnosis or "雙重惡化" in r.GM_Diagnosis:
        trend_score = 0.0
    else:
        trend_score = 25.0
    roic_score = _bounded_score(r.ROIC_pct, 5.0, 20.0, missing=35.0)
    if r.Real_FCF_Years_Available >= 3:
        positive_score = safe_div(r.Real_FCF_Positive_Years_5Y, r.Real_FCF_Years_Available, 0.0) * 100.0
    else:
        positive_score = 40.0
    if math.isfinite(r.Real_FCF_Margin_Std_5Y_pct):
        margin_stability = 100.0 if r.Real_FCF_Margin_Std_5Y_pct <= 5.0 else 75.0 if r.Real_FCF_Margin_Std_5Y_pct <= 10.0 else 40.0 if r.Real_FCF_Margin_Std_5Y_pct <= 20.0 else 10.0
    else:
        margin_stability = 40.0
    conversion_score = _bounded_score(r.OCF_to_NetIncome_5Y, 0.5, 1.2, missing=40.0)
    stability_score = positive_score * 0.55 + margin_stability * 0.25 + conversion_score * 0.20
    quality_score = icr_score * 0.25 + fcf_quality * 0.20 + trend_score * 0.20 + roic_score * 0.20 + stability_score * 0.15

    expectations_score = _expectations_score(r.Implied_EBITDA_CAGR_3Y_pct, r.ROIC_pct, r.GM_3Q_Change_pp)
    momentum_score = _bounded_score(r.Momentum_12M_pct, -30.0, 30.0, missing=50.0)
    inflection_score = r.Operating_Inflection_Score if math.isfinite(r.Operating_Inflection_Score) else 50.0

    risk_penalty = 0.0
    if math.isfinite(r.EBITDA_Drawdown_30_pct):
        if r.EBITDA_Drawdown_30_pct <= -80.0:
            risk_penalty += 35.0
        elif r.EBITDA_Drawdown_30_pct <= -60.0:
            risk_penalty += 25.0
        elif r.EBITDA_Drawdown_30_pct <= -40.0:
            risk_penalty += 15.0
        elif r.EBITDA_Drawdown_30_pct <= -25.0:
            risk_penalty += 5.0
    else:
        risk_penalty += 10.0
    if r.Dilution_Illusion:
        risk_penalty += 5.0
    if r.Persistent_Dilution:
        risk_penalty += 25.0
    if math.isfinite(r.OCF_3Y_Cumulative_B) and r.OCF_3Y_Cumulative_B <= 0:
        risk_penalty += 15.0
    if "結構性價值陷阱" in r.GM_Diagnosis:
        risk_penalty += 25.0
    if "雙重惡化" in r.GM_Diagnosis:
        risk_penalty += 35.0
    if r.Squeeze_Risk:
        risk_penalty += 8.0
    if "庫存惡化" in r.Inventory_Signal:
        risk_penalty += 8.0
    if math.isfinite(r.Stress_ICR_30x):
        if r.Stress_ICR_30x < 1.0:
            risk_penalty += 25.0
        elif r.Stress_ICR_30x < STRESS_ICR_MIN:
            risk_penalty += 15.0
        elif r.Stress_ICR_30x < 2.0:
            risk_penalty += 8.0
    if math.isfinite(r.NetDebt_to_Stress_EBITDA_30x):
        if r.NetDebt_to_Stress_EBITDA_30x > 5.0:
            risk_penalty += 15.0
        elif r.NetDebt_to_Stress_EBITDA_30x > 4.0:
            risk_penalty += 8.0
    long_term_score = (
        quality_score * 0.35
        + value_score * 0.30
        + expectations_score * 0.20
        + momentum_score * 0.05
        + inflection_score * 0.05
        + r.Capital_Allocation_Score * 0.05
        - risk_penalty
    )
    return {
        "value_score": round(value_score, 2),
        "quality_score": round(quality_score, 2),
        "expectations_score": round(expectations_score, 2),
        "inflection_score": round(inflection_score, 2),
        "risk_penalty": round(risk_penalty, 2),
        "long_term_score": round(max(0.0, min(100.0, long_term_score)), 2),
        "implied_cagr_limit": round(dynamic_implied_cagr_limit(r.ROIC_pct, r.GM_3Q_Change_pp), 2),
    }


def apply_long_term_framework(r: "ModeCResult") -> "ModeCResult":
    if not r.Decision_State:
        if str(r.Status).startswith("Pass"):
            r.Decision_State = "PASS"
        elif str(r.Status).startswith("Abstain"):
            r.Decision_State = "ABSTAIN"
        else:
            r.Decision_State = "FAIL"
    if r.Decision_State != "FAIL" and (
        not r.Model_Supported or r.Data_Confidence_Score < MIN_DATA_CONFIDENCE
    ):
        r.Decision_State = "ABSTAIN"
        if r.Status == "Pass":
            r.Status = "Abstain: model support or data confidence gate"
    if r.Decision_State == "ABSTAIN":
        r.Value_Score = np.nan
        r.Quality_Score = np.nan
        r.Expectations_Score = np.nan
        r.Operating_Inflection_Score = np.nan
        r.Risk_Penalty = np.nan
        r.Long_Term_Score = np.nan
        r.Long_Term_Eligible = False
        r.Verdict = "暫不判斷：資料信心不足或模型不適用"
        r.Research_Action = "不得建立部位；先補齊來源證據或專用產業模型"
        r.Suggested_Starter_Weight_pct_Total = 0.0
        r.Trade_Tool = r.Research_Action
        return r
    scores = calculate_long_term_scores(r)
    r.Value_Score = scores["value_score"]
    r.Quality_Score = scores["quality_score"]
    r.Expectations_Score = scores["expectations_score"]
    r.Operating_Inflection_Score = scores["inflection_score"]
    r.Risk_Penalty = scores["risk_penalty"]
    r.Long_Term_Score = scores["long_term_score"]
    r.Implied_CAGR_Limit_pct = scores["implied_cagr_limit"]
    r.Implied_CAGR_Headroom_pct = (
        r.Implied_CAGR_Limit_pct - r.Implied_EBITDA_CAGR_3Y_pct
        if math.isfinite(r.Implied_EBITDA_CAGR_3Y_pct)
        else np.nan
    )

    expectations_ok = math.isfinite(r.Implied_EBITDA_CAGR_3Y_pct) and r.Expectations_Score >= 15.0
    drawdown_ok = math.isfinite(r.EBITDA_Drawdown_30_pct) and r.EBITDA_Drawdown_30_pct > -75.0
    stress_survival_ok = bool(r.Stress_Survival_30)
    trend_ok = not any(x in r.GM_Diagnosis for x in ["結構性價值陷阱", "雙重惡化", "資料不足"])
    required_positive_fcf_years = minimum_positive_fcf_years(
        r.Real_FCF_Years_Available
    )
    fcf_history_ok = (
        r.Real_FCF_Positive_Years_5Y >= required_positive_fcf_years
    )
    ocf_history_ok = not (r.OCF_3Y_Years >= 3 and math.isfinite(r.OCF_3Y_Cumulative_B) and r.OCF_3Y_Cumulative_B <= 0)
    r.Long_Term_Eligible = bool(
        r.Status == "Pass"
        and r.Decision_State == "PASS"
        and r.Model_Supported
        and r.Data_Confidence_Score >= MIN_DATA_CONFIDENCE
        and r.Long_Term_Score >= MIN_LONG_TERM_SCORE
        and math.isfinite(r.EV_EBITDA_10Y_Percentile)
        and r.Real_FCF_Yield_pct >= 2.0
        and (math.isinf(r.ICR) or r.ICR >= ICR_WARNING)
        and not r.Persistent_Dilution
        and expectations_ok
        and drawdown_ok
        and stress_survival_ok
        and trend_ok
        and fcf_history_ok
        and ocf_history_ok
    )

    if r.Long_Term_Eligible and r.Long_Term_Score >= HIGH_PRIORITY_SCORE:
        r.Verdict = "高優先研究：品質、估值與資本配置達標"
        r.Research_Action = "完成 AI 九項覆核；若屬 QQQ/VOO 前十大亦達 80 分，可考慮 1.5% 總資產起始部位"
        r.Suggested_Starter_Weight_pct_Total = STARTER_WEIGHT_PCT_TOTAL
    elif r.Long_Term_Eligible and r.Long_Term_Score >= SMALL_POSITION_SCORE:
        r.Verdict = "可考慮小部位：仍需完成論點與 ETF 重疊覆核"
        r.Research_Action = "完成研究後可考慮 1.0% 總資產起始部位；若為 QQQ/VOO 前十大則須等分數達 80"
        r.Suggested_Starter_Weight_pct_Total = STARTER_WEIGHT_MIN_PCT_TOTAL
    elif r.Long_Term_Eligible and r.Long_Term_Score >= RESEARCH_PRIORITY_SCORE:
        r.Verdict = "優先研究：尚未達買入分數"
        r.Research_Action = "先完成研究與財報驗證，不建立部位"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    elif r.Long_Term_Eligible:
        r.Verdict = "研究候選：60 分以上不等於可買"
        r.Research_Action = "列入觀察；等待品質、估值或證據改善至 75 分以上"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    elif r.Status == "Pass":
        r.Verdict = "觀察：未達長期研究門檻"
        r.Research_Action = "不自動買入；等待品質、估值或下檔風險改善"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    else:
        r.Verdict = "排除"
        r.Research_Action = "不進入主動投資研究池"
        r.Suggested_Starter_Weight_pct_Total = 0.0
    r.Trade_Tool = r.Research_Action
    return r


def composite_score_for_result(r: "ModeCResult") -> float:
    """Backward-compatible sort key: lower is better."""
    return 100.0 - calculate_long_term_scores(r)["long_term_score"]


def select_diversified_shortlist(
    results: List["ModeCResult"],
    target_size: int = TARGET_SHORTLIST_SIZE,
    max_per_sector: int = MAX_PER_SECTOR,
) -> List["ModeCResult"]:
    candidates = sorted(
        [r for r in results if r.Status == "Pass" and r.Long_Term_Eligible],
        key=lambda r: (-r.Long_Term_Score, r.Ticker),
    )
    selected: List[ModeCResult] = []
    sector_counts: Dict[str, int] = {}
    for r in candidates:
        sector = (r.Sector or "Unknown").strip() or "Unknown"
        if max_per_sector > 0 and sector_counts.get(sector, 0) >= max_per_sector:
            continue
        selected.append(r)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= target_size:
            break
    return selected


def _specialized_balance(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
    *,
    missing_value: float = np.nan,
) -> float:
    if frame.empty:
        return missing_value
    return sec.latest_balance(frame, metric_name)


def _specialized_average_balance(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
) -> float:
    facts = sec._instant_facts(frame)
    if facts.empty:
        return np.nan
    latest = facts.iloc[-1]
    latest_end = pd.Timestamp(latest["end"])
    age_days = (latest_end - pd.to_datetime(facts["end"], errors="coerce")).dt.days
    prior_candidates = facts[age_days.between(300, 450, inclusive="both")]
    selected = (
        pd.DataFrame([prior_candidates.iloc[-1], latest])
        if not prior_candidates.empty
        else pd.DataFrame([latest])
    )
    sec._mark_rows_used(selected, f"{metric_name}:average-balance")
    return float(pd.to_numeric(selected["val"], errors="coerce").mean()) / 1e9


def _specialized_scalar(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
) -> float:
    facts = sec._instant_facts(frame)
    if facts.empty:
        return np.nan
    row = facts.iloc[-1]
    sec._mark_rows_used(row, f"{metric_name}:latest-scalar")
    return float(row["val"])


def _ratio_as_percent(value: float) -> float:
    if not math.isfinite(value):
        return np.nan
    return value * 100.0 if abs(value) <= 1.5 else value


def _specialized_ttm(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
    *,
    signed: bool = True,
) -> Tuple[float, str, Dict[str, object]]:
    if frame.empty:
        return np.nan, "missing", {}
    return sec.ttm_flow(frame, signed=signed, normalized_metric=metric_name)


def _specialized_growth_pct(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
) -> float:
    if frame.empty:
        return np.nan
    quarterly = sec.quarterly_series(frame, metric_name)
    if len(quarterly) >= 8:
        current = float(quarterly.tail(4).sum())
        previous = float(quarterly.iloc[-8:-4].sum())
        return (current / previous - 1.0) * 100.0 if previous > 0 else np.nan
    annual = sec._annual_facts(frame)
    if len(annual) >= 2:
        selected = annual.tail(2)
        sec._mark_rows_used(selected, f"{metric_name}:annual-growth")
        previous = float(selected.iloc[0]["val"])
        current = float(selected.iloc[1]["val"])
        return (current / previous - 1.0) * 100.0 if previous > 0 else np.nan
    return np.nan


def _specialized_balance_growth_pct(
    sec: SECDataDistiller,
    frame: pd.DataFrame,
    metric_name: str,
) -> float:
    facts = sec._instant_facts(frame)
    if len(facts) < 2:
        return np.nan
    latest = facts.iloc[-1]
    age_days = (
        pd.Timestamp(latest["end"])
        - pd.to_datetime(facts["end"], errors="coerce")
    ).dt.days
    prior_candidates = facts[age_days.between(300, 450, inclusive="both")]
    if prior_candidates.empty:
        return np.nan
    prior = prior_candidates.iloc[-1]
    sec._mark_rows_used(pd.DataFrame([prior, latest]), f"{metric_name}:balance-growth")
    previous = float(prior["val"])
    current = float(latest["val"])
    return (current / previous - 1.0) * 100.0 if previous > 0 else np.nan


CONSOLIDATED_DEBT_TAGS = {
    "DebtCurrentAndLongTerm",
    "DebtAndFinanceLeaseObligations",
}
LONG_TERM_ONLY_DEBT_TAGS = {
    "LongTermDebt",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndFinanceLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    "LongTermDebtNoncurrent",
}
DEBT_TAGS_INCLUDING_FINANCE_LEASE = {
    "DebtAndFinanceLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndFinanceLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
}
DEBT_CURRENT_TAGS_INCLUDING_FINANCE_LEASE = {
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "LongTermDebtAndFinanceLeaseObligationsCurrent",
}


def _compose_total_debt(
    debt_total: float,
    debt_total_concept: str,
    debt_current: float = 0.0,
    debt_current_concept: str = "",
    short_term_total: float = 0.0,
    short_term_total_concept: str = "",
    other_short_term: float = 0.0,
    commercial_paper: float = 0.0,
    finance_lease: float = 0.0,
) -> float:
    if not math.isfinite(debt_total) or not debt_total_concept:
        return np.nan
    short_term = (
        max(short_term_total, 0.0)
        if short_term_total_concept
        else max(other_short_term, 0.0) + max(commercial_paper, 0.0)
    )
    finance_lease_addition = (
        0.0
        if (
            debt_total_concept in DEBT_TAGS_INCLUDING_FINANCE_LEASE
            or debt_current_concept in DEBT_CURRENT_TAGS_INCLUDING_FINANCE_LEASE
        )
        else max(finance_lease, 0.0)
    )
    if debt_total_concept in CONSOLIDATED_DEBT_TAGS:
        return max(debt_total, 0.0) + finance_lease_addition
    if debt_total_concept == "NotesPayable":
        return max(debt_total, 0.0) + short_term + finance_lease_addition
    current_component = max(debt_current, 0.0)
    if debt_current_concept in {"DebtCurrent", "ShortTermBorrowings"}:
        short_term = 0.0
    return max(debt_total, 0.0) + current_component + short_term + finance_lease_addition


def _specialized_total_debt(
    sec: SECDataDistiller,
    frames: Mapping[str, pd.DataFrame],
) -> Tuple[float, str, List[str]]:
    def latest(
        name: str,
        reference_end: Optional[pd.Timestamp] = None,
    ) -> Tuple[float, str, Optional[pd.Timestamp], Optional[pd.Series]]:
        source = frames.get(name, pd.DataFrame())
        if source.empty:
            return 0.0, "", None, None
        facts = sec._instant_facts(source)
        if facts.empty:
            return 0.0, "", None, None
        if reference_end is not None:
            period_ends = pd.to_datetime(facts["end"], errors="coerce")
            aligned = facts[(period_ends - reference_end).abs() <= pd.Timedelta(days=45)]
            if aligned.empty:
                return 0.0, "", None, None
            facts = aligned
        row = facts.iloc[-1]
        period_end = pd.to_datetime(row.get("end"), errors="coerce")
        if pd.isna(period_end):
            return 0.0, "", None, None
        age_days = (
            sec.decision_timestamp.normalize() - pd.Timestamp(period_end).normalize()
        ).days
        if age_days > fact_age_limit_days(facts):
            return 0.0, "", None, None
        return (
            float(row["val"]) / 1e9,
            str(row.get("concept") or ""),
            pd.Timestamp(period_end),
            row,
        )

    debt_total, debt_total_concept, debt_period_end, debt_total_row = latest("DebtTotal")
    debt_current, debt_current_concept, _, debt_current_row = latest("DebtCurrent", debt_period_end)
    short_total, short_total_concept, _, short_total_row = latest("DebtShortTermTotal", debt_period_end)
    other_short, other_short_concept, _, other_short_row = latest("DebtOtherShortTerm", debt_period_end)
    commercial_paper, commercial_paper_concept, _, commercial_paper_row = latest(
        "DebtCommercialPaper", debt_period_end
    )
    finance_lease, finance_lease_concept, _, finance_lease_row = latest(
        "DebtFinanceLease", debt_period_end
    )
    total = _compose_total_debt(
        debt_total,
        debt_total_concept,
        debt_current,
        debt_current_concept,
        short_total,
        short_total_concept,
        other_short,
        commercial_paper,
        finance_lease,
    )
    source_concepts = ["DebtTotal"] if debt_total_concept else []
    if debt_total_concept not in CONSOLIDATED_DEBT_TAGS:
        if debt_total_concept in LONG_TERM_ONLY_DEBT_TAGS and debt_current_concept:
            source_concepts.append("DebtCurrent")
        if debt_current_concept not in {"DebtCurrent", "ShortTermBorrowings"}:
            if short_total_concept:
                source_concepts.append("DebtShortTermTotal")
            else:
                if other_short_concept:
                    source_concepts.append("DebtOtherShortTerm")
                if commercial_paper_concept:
                    source_concepts.append("DebtCommercialPaper")
    if (
        finance_lease_concept
        and debt_total_concept not in DEBT_TAGS_INCLUDING_FINANCE_LEASE
        and debt_current_concept not in DEBT_CURRENT_TAGS_INCLUDING_FINANCE_LEASE
    ):
        source_concepts.append("DebtFinanceLease")
    source_rows = {
        "DebtTotal": debt_total_row,
        "DebtCurrent": debt_current_row,
        "DebtShortTermTotal": short_total_row,
        "DebtOtherShortTerm": other_short_row,
        "DebtCommercialPaper": commercial_paper_row,
        "DebtFinanceLease": finance_lease_row,
    }
    for concept_name in source_concepts:
        source_row = source_rows.get(concept_name)
        if source_row is not None:
            sec._mark_rows_used(source_row, f"{concept_name}:latest-debt-component")
    method = (
        f"{debt_total_concept} plus non-overlapping current/short-term/lease components"
        if debt_total_concept
        else "missing"
    )
    return total, method, source_concepts


def _specialized_ppe_capex_proxy(
    sec: SECDataDistiller,
    ppe_frame: pd.DataFrame,
    dna_ttm: float,
) -> float:
    facts = sec._instant_facts(ppe_frame)
    if facts.empty or not math.isfinite(dna_ttm):
        return np.nan
    latest = facts.iloc[-1]
    latest_end = pd.Timestamp(latest["end"])
    age_days = (latest_end - pd.to_datetime(facts["end"], errors="coerce")).dt.days
    prior_candidates = facts[age_days.between(300, 450, inclusive="both")]
    if prior_candidates.empty:
        return np.nan
    prior = prior_candidates.iloc[-1]
    selected = pd.DataFrame([prior, latest])
    ppe_change = (float(latest["val"]) - float(prior["val"])) / 1e9
    proxy = ppe_change + abs(float(dna_ttm))
    if proxy <= 0:
        return np.nan
    source_ids = sec._mark_rows_used(selected, "Utility_CapEx_Proxy:ppe-roll-forward")
    sec._record_derived_from_ids(
        "TTM_CapEx_PPERollForward",
        proxy,
        "USD_B",
        "year-over-year change in net PP&E + TTM D&A",
        [
            *source_ids,
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(sec.ticker, "TTM_DnA"),
        ],
        "Utility_CapEx_Proxy:model-input",
    )
    return proxy


def _specialized_ebitda_history(
    sec: SECDataDistiller,
    ebit_frame: pd.DataFrame,
    dna_frame: pd.DataFrame,
) -> List[float]:
    ebit_annual = sec._annual_facts(ebit_frame)
    dna_annual = sec._annual_facts(dna_frame)
    if ebit_annual.empty or dna_annual.empty:
        return []
    ebit_by_end = {pd.Timestamp(row["end"]): row for _, row in ebit_annual.iterrows()}
    dna_by_end = {pd.Timestamp(row["end"]): row for _, row in dna_annual.iterrows()}
    common_ends = sorted(set(ebit_by_end) & set(dna_by_end))
    if not common_ends:
        return []
    consecutive_desc = [common_ends[-1]]
    for period_end in reversed(common_ends[:-1]):
        gap_days = (consecutive_desc[-1] - period_end).days
        if 300 <= gap_days <= 450:
            consecutive_desc.append(period_end)
        elif gap_days > 450:
            break
    values = []
    for period_end in sorted(consecutive_desc[:10]):
        ebit_row = ebit_by_end[period_end]
        dna_row = dna_by_end[period_end]
        sec._mark_rows_used(ebit_row, "Cyclical_EBITDA:annual-history")
        sec._mark_rows_used(dna_row, "Cyclical_EBITDA:annual-history")
        values.append((float(ebit_row["val"]) + abs(float(dna_row["val"]))) / 1e9)
    return values


@dataclass
class ModeCResult:
    Ticker: str
    Status: str
    Scoring_Framework: str = "GENERAL_CORPORATE_V13"
    Input_Security_Class: str = ""
    Security_Class_Confidence: str = ""
    Security_Class_Evidence_Source: str = ""
    Initial_Industry_Model_Key: str = ""
    Model_Route_Refined: bool = False
    Price: float = np.nan
    Sector: str = ""
    Industry: str = ""
    Model_Route: str = "GENERAL_CORPORATE"
    Model_Supported: bool = True
    Model_Route_Reason: str = ""
    Industry_Model_Key: str = "GENERAL_CORPORATE"
    Industry_Model_Decision: str = ""
    Industry_Model_Score: float = np.nan
    Industry_Model_Coverage: float = np.nan
    Industry_Model_Metrics_JSON: str = ""
    Industry_Model_Components_JSON: str = ""
    Industry_Model_Warnings: str = ""
    Industry_Model_Hard_Failures: str = ""
    Industry_Model_Required_Missing: str = ""
    Industry_Model_Optional_Missing: str = ""
    Decision_State: str = ""
    Decision_Timestamp: str = ""
    Data_Confidence_Score: float = 100.0
    Data_Confidence_Reasons: str = ""
    Evidence_Source_Count: float = 0.0
    Evidence_AcceptedAt_Ratio: float = np.nan
    MarketCap_B: float = np.nan
    EV_B: float = np.nan
    Total_Debt_B: float = np.nan
    Cash_B: float = np.nan
    Net_Debt_B: float = np.nan
    Debt_Source_Method: str = ""
    Maintenance_Real_FCF_B: float = np.nan
    Conservative_Real_FCF_B: float = np.nan
    Real_FCF_Yield_pct: float = np.nan
    Conservative_Real_FCF_Yield_pct: float = np.nan
    Maintenance_Real_FCF_to_MarketCap_Yield_pct: float = np.nan
    Conservative_Real_FCF_to_MarketCap_Yield_pct: float = np.nan
    Maintenance_Real_FCF_to_EV_Yield_pct: float = np.nan
    Conservative_Real_FCF_to_EV_Yield_pct: float = np.nan
    Maintenance_Real_FCF_Yield_Low_pct: float = np.nan
    Maintenance_Real_FCF_Yield_High_pct: float = np.nan
    TTM_OCF_B: float = np.nan
    Dynamic_CapEx_B: float = np.nan
    Maintenance_CapEx_B: float = np.nan
    Maintenance_CapEx_Low_B: float = np.nan
    Maintenance_CapEx_High_B: float = np.nan
    Maintenance_CapEx_Confidence: str = ""
    Growth_CapEx_B: float = np.nan
    CapEx_to_DnA_x: float = np.nan
    CapEx_Reinvestment_Method: str = ""
    TTM_SBC_B: float = np.nan
    SBC_Economic_Cost_B: float = np.nan
    Net_Buyback_Yield_pct: float = np.nan
    Buyback_Offset_Effective: bool = False
    Per_Share_FCF_CAGR_3Y_pct: float = np.nan
    Per_Share_EPS_CAGR_3Y_pct: float = np.nan
    SBC_Attribution_JSON: str = ""
    ICR: float = np.nan
    ICR_Method: str = ""
    Real_Buyback_B: float = np.nan
    Share_Count_Change_pct: float = np.nan
    Share_Count_Change_3Y_pct: float = np.nan
    Share_Split_Factor_1Y: float = 1.0
    Share_Split_Factor_3Y: float = 1.0
    Share_Basis_Discontinuity: bool = False
    Dilution_Illusion: bool = False
    Persistent_Dilution: bool = False
    ROIC_pct: float = np.nan
    ROCE_pct: float = np.nan
    OCF_3Y_Cumulative_B: float = np.nan
    OCF_3Y_Years: float = 0.0
    Real_FCF_Positive_Years_5Y: float = 0.0
    Real_FCF_Years_Available: float = 0.0
    Real_FCF_Margin_Std_5Y_pct: float = np.nan
    OCF_to_NetIncome_5Y: float = np.nan
    Real_FCF_to_NetIncome_5Y: float = np.nan
    Capital_Allocation_Score: float = 50.0
    EBITDA_B: float = np.nan
    EV_EBITDA_x: float = np.nan
    PE_x: float = np.nan
    EV_EBITDA_10Y_Percentile: float = np.nan
    PE_10Y_Percentile: float = np.nan
    Historical_Valuation_Valid_Years: float = 0.0
    Historical_Valuation_Total_Years: float = 0.0
    Historical_Valuation_Coverage: float = np.nan
    Historical_Valuation_Status: str = ""
    Point_in_Time_FX_Rate: float = np.nan
    ADR_Ratio: float = np.nan
    Industry_Stress_Extension_Status: str = "NOT_IMPLEMENTED"
    Industry_Stress_Extension_Reason: str = "Generic EBITDA stress is only applicable to GENERAL_CORPORATE"
    EBITDA_Drawdown_15_pct: float = np.nan
    EBITDA_Drawdown_30_pct: float = np.nan
    Stress_ICR_15x: float = np.nan
    Stress_ICR_30x: float = np.nan
    NetDebt_to_Stress_EBITDA_30x: float = np.nan
    Stress_Real_FCF_30_B: float = np.nan
    Stress_Survival_30: bool = False
    GM_Diagnosis: str = ""
    Rev_3Q_Change_pct: float = np.nan
    GM_Latest_pct: float = np.nan
    GM_3Q_Change_pp: float = np.nan
    Implied_EBITDA_CAGR_3Y_pct: float = np.nan
    Implied_CAGR_Limit_pct: float = np.nan
    Implied_CAGR_Headroom_pct: float = np.nan
    Reverse_DCF_Required_Return_pct: float = REVERSE_DCF_REQUIRED_RETURN * 100
    Momentum_12M_pct: float = np.nan
    Value_Score: float = np.nan
    Quality_Score: float = np.nan
    Expectations_Score: float = np.nan
    Operating_Inflection_Score: float = 50.0
    Risk_Penalty: float = np.nan
    Long_Term_Score: float = np.nan
    Long_Term_Eligible: bool = False
    Research_Action: str = ""
    Suggested_Starter_Weight_pct_Total: float = 0.0
    Physical_Check: str = "依產業分類動態產生；非半導體/AI/CSP 不套用 TSMC/ASML/CSP。"
    ShortInterest_pctFloat: float = np.nan
    DaysToCover: float = np.nan
    Short_Data_Age_Days: float = np.nan
    Squeeze_Risk: bool = False
    DSI_Latest: float = np.nan
    DSI_QoQ_Change_pct: float = np.nan
    DSI_YoY_Change_pct: float = np.nan
    DSI_2Q_Down: bool = False
    Inventory_Inflection: bool = False
    Inventory_Signal: str = "資料不足或不適用"
    Catalysts_30D: str = ""
    Data_Quality_Flags: str = ""
    Verdict: str = ""
    Trade_Tool: str = ""
    Agent_Tasks: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.Decision_State:
            return
        if self.Status == "Pass":
            self.Decision_State = "PASS"
        elif self.Status.startswith("Abstain"):
            self.Decision_State = "ABSTAIN"
        elif self.Status.startswith("Error"):
            self.Decision_State = "ABSTAIN"
        elif self.Status.startswith("Fail"):
            self.Decision_State = "FAIL"


SPECIALIZED_CONCEPTS = {
    "BANK": {"AOCI", "CreditLossAllowance", "CreditLossProvision", "Deposits", "Loans", "NetInterestIncome", "Tier1Ratio", "Tier1WellCapitalizedMinimum"},
    "INSURANCE_P_AND_C": {"InsuranceClaims", "InsuranceCombinedExpense", "NetInvestmentIncome", "PolicyholderBenefits", "PremiumsEarned", "PremiumsWritten", "ReserveDevelopment", "UnderwritingExpense"},
    "INSURANCE_LIFE": {"InsuranceClaims", "InsuranceCombinedExpense", "NetInvestmentIncome", "PolicyholderBenefits", "PremiumsEarned", "PremiumsWritten", "ReserveDevelopment", "UnderwritingExpense"},
    "REIT_EQUITY": {"DnA", "Dividend", "GainOnPropertySale", "IncomeTaxExpenseBenefit", "Interest", "LeaseRevenue", "RealEstateCapEx", "RealEstateImpairment"},
    "REIT_MORTGAGE": {"Dividend", "NetInterestIncome"},
    "REGULATED_UTILITY": {"CapEx", "Dividend", "DnA", "EBIT", "Interest", "OCF", "PPENet"},
    "CYCLICAL_MIDCYCLE": {"CapEx", "DnA", "EBIT", "Interest", "OCF", "Revenue", "SBC"},
    "FINANCIAL_LENDER": {"CreditLossAllowance", "Loans", "NetInterestIncome"},
    "FINANCIAL_FEE": {"CreditLossAllowance", "DnA", "EBIT", "Loans", "NetInterestIncome", "OCF", "Revenue"},
}


SPECIALIZED_RECENCY_CONCEPTS = {
    "BANK": {
        "assets_b": ("Assets",),
        "tangible_equity_b": ("Equity", "Goodwill", "IntangibleAssets"),
        "net_income_ttm_b": ("NetIncome",),
        "deposits_b": ("Deposits",),
        "loans_b": ("Loans",),
        "credit_loss_allowance_b": ("CreditLossAllowance",),
        "net_interest_income_growth_pct": ("NetInterestIncome",),
        "aoci_b": ("AOCI",),
        "tier1_ratio_pct": ("Tier1Ratio",),
        "tier1_well_capitalized_min_pct": ("Tier1WellCapitalizedMinimum",),
    },
    "INSURANCE_P_AND_C": {
        "premiums_earned_ttm_b": ("PremiumsEarned",),
        "combined_expense_ttm_b": ("InsuranceCombinedExpense",),
        "claims_incurred_ttm_b": ("InsuranceClaims",),
        "underwriting_expense_ttm_b": ("UnderwritingExpense",),
        "premium_growth_pct": ("PremiumsEarned",),
        "reserve_development_to_premium_pct": ("ReserveDevelopment",),
        "assets_b": ("Assets",),
        "equity_b": ("Equity",),
        "net_income_ttm_b": ("NetIncome",),
    },
    "INSURANCE_LIFE": {
        "premiums_earned_ttm_b": ("PremiumsEarned",),
        "net_investment_income_ttm_b": ("NetInvestmentIncome",),
        "policyholder_benefits_ttm_b": ("PolicyholderBenefits",),
        "premium_growth_pct": ("PremiumsEarned",),
        "assets_b": ("Assets",),
        "equity_b": ("Equity",),
        "net_income_ttm_b": ("NetIncome",),
    },
    "REIT_EQUITY": {
        "net_income_ttm_b": ("NetIncome",),
        "real_estate_dna_ttm_b": ("DnA",),
        "maintenance_capex_b": ("RealEstateCapEx", "DnA"),
        "interest_ttm_b": ("Interest",),
        "debt_b": ("DebtTotal",),
        "cash_b": ("Cash",),
        "dividends_ttm_b": ("Dividend",),
        "lease_revenue_growth_pct": ("LeaseRevenue",),
    },
    "REIT_MORTGAGE": {
        "assets_b": ("Assets",),
        "equity_b": ("Equity",),
        "net_income_ttm_b": ("NetIncome",),
        "net_interest_income_ttm_b": ("NetInterestIncome",),
        "dividends_ttm_b": ("Dividend",),
        "net_interest_income_growth_pct": ("NetInterestIncome",),
    },
    "REGULATED_UTILITY": {
        "ebit_ttm_b": ("EBIT",),
        "interest_ttm_b": ("Interest",),
        "debt_b": ("DebtTotal",),
        "equity_b": ("Equity",),
        "ocf_ttm_b": ("OCF",),
        "capex_ttm_b": ("CapEx", "PPENet", "DnA"),
        "net_income_ttm_b": ("NetIncome",),
        "dividends_ttm_b": ("Dividend",),
        "ppe_growth_pct": ("PPENet",),
    },
    "CYCLICAL_MIDCYCLE": {
        "current_ebitda_b": ("EBIT", "DnA"),
        "debt_b": ("DebtTotal",),
        "cash_b": ("Cash",),
        "interest_ttm_b": ("Interest",),
        "dna_ttm_b": ("DnA",),
        "real_fcf_ttm_b": ("OCF", "CapEx", "DnA", "Revenue", "SBC"),
        "revenue_growth_pct": ("Revenue",),
    },
    "FINANCIAL_LENDER": {
        "assets_b": ("Assets",),
        "tangible_equity_b": ("Equity", "Goodwill", "IntangibleAssets"),
        "net_income_ttm_b": ("NetIncome",),
        "loans_b": ("Loans",),
        "credit_loss_allowance_b": ("CreditLossAllowance",),
        "net_interest_income_growth_pct": ("NetInterestIncome",),
    },
    "FINANCIAL_FEE": {
        "revenue_ttm_b": ("Revenue",),
        "ebit_ttm_b": ("EBIT",),
        "ocf_ttm_b": ("OCF",),
        "net_income_ttm_b": ("NetIncome",),
        "tangible_equity_b": ("Equity", "Goodwill", "IntangibleAssets"),
        "assets_b": ("Assets",),
        "ebitda_ttm_b": ("EBIT", "DnA"),
        "revenue_growth_pct": ("Revenue",),
        "debt_b": ("DebtTotal",),
        "cash_b": ("Cash",),
    },
}


SPECIALIZED_DISCLOSURE_GAPS = {
    "BANK": ["CET1 exact ratio", "uninsured deposits", "nonperforming-loan ratio"],
    "INSURANCE_P_AND_C": ["statutory RBC capital", "investment duration / credit buckets"],
    "INSURANCE_LIFE": ["statutory RBC capital", "asset-liability duration matching"],
    "REIT_EQUITY": ["same-store NOI", "occupancy", "fixed-rate debt and maturity ladder"],
    "REIT_MORTGAGE": [
        "company-defined recurring earnings / dividend coverage",
        "repo haircut / margin-call schedule",
        "duration gap",
    ],
    "REGULATED_UTILITY": ["allowed ROE", "filed rate base", "regulatory lag"],
    "CYCLICAL_MIDCYCLE": ["commodity cost curve", "reserve life / replacement economics"],
    "FINANCIAL_LENDER": ["delinquency vintage", "warehouse covenant headroom"],
    "FINANCIAL_FEE": ["AUM or client-asset flows", "regulatory net capital"],
}


SPECIALIZED_AGENT_TASKS = {
    "BANK": [
        "Verify CET1 and total capital ratios in the latest regulatory filing",
        "Check uninsured deposits, deposit beta, nonperforming loans and charge-offs",
        "Reconcile AOCI and held-to-maturity unrealized losses against tangible equity",
    ],
    "INSURANCE_P_AND_C": [
        "Verify reported combined ratio and accident-year loss ratio",
        "Check prior-year reserve development and reinsurance counterparty exposure",
        "Review investment portfolio duration, credit quality and catastrophe exposure",
    ],
    "INSURANCE_LIFE": [
        "Verify statutory RBC and asset-liability duration matching",
        "Check surrender behavior, spread compression and reserve assumptions",
        "Review investment portfolio credit losses and reinsurance dependence",
    ],
    "REIT_EQUITY": [
        "Verify company-defined AFFO against the Nareit FFO bridge",
        "Check same-store NOI, occupancy, lease expirations and tenant concentration",
        "Review debt maturities, fixed-rate share and secured debt restrictions",
    ],
    "REIT_MORTGAGE": [
        "Verify book value after the latest quarter and subsequent rate moves",
        "Check repo funding, haircuts, hedges, duration gap and margin-call liquidity",
        "Reconcile dividend coverage with recurring net interest income",
    ],
    "REGULATED_UTILITY": [
        "Verify allowed ROE, filed rate base and pending rate cases",
        "Check regulatory lag, fuel recovery mechanisms and customer affordability",
        "Review capex funding, equity issuance needs and debt maturity wall",
    ],
    "CYCLICAL_MIDCYCLE": [
        "Validate mid-cycle volume, price and margin assumptions against the cost curve",
        "Check reserve life, replacement capex and trough covenant headroom",
        "Separate structural demand change from a normal inventory cycle",
    ],
    "FINANCIAL_LENDER": [
        "Check delinquency vintages, charge-offs, funding facilities and covenants",
        "Verify allowance coverage by product and underwriting cohort",
        "Review securitization, warehouse and refinancing concentration",
    ],
    "FINANCIAL_FEE": [
        "Verify net client flows, fee rates, client concentration and operating leverage",
        "Check regulatory net capital and off-balance-sheet obligations",
        "Separate market appreciation from organic fee-base growth",
    ],
}


def _specialized_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _specialized_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_specialized_json(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def run_specialized_mode_c_pipeline(
    ticker: str,
    cik: str,
    email: str,
    pm: dict,
    info: dict,
    model_route: Dict[str, object],
    decision_timestamp: pd.Timestamp,
) -> ModeCResult:
    model_key = str(model_route.get("model_key") or "UNKNOWN")
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    price = first_finite_positive(
        info.get("currentPrice"),
        info.get("regularMarketPrice"),
        pm.get("last_close"),
    )
    if not math.isfinite(price) or price <= 0:
        return ModeCResult(Ticker=ticker, Status="Fail: 價格失真")

    sec = SECDataDistiller(email, ticker=ticker, cik=cik, decision_timestamp=decision_timestamp)
    price_evidence_id = sec.record_observed_input(
        "MarketPrice", price, "USD_per_share", "YAHOO_FINANCE", "Close/currentPrice"
    )
    sec.record_observed_input(
        "DollarVolume30D", pm.get("dollar_volume"), "USD", "YAHOO_FINANCE", "mean(Close*Volume,30D)"
    )
    metadata_fallback_fields = set(
        info.get("_verifiedUniverseMetadataFallbackFields") or []
    )
    sec.record_observed_input(
        "Sector", sector, "text",
        "MONTHLY_HUNTER_CACHE" if "sector" in metadata_fallback_fields else "YAHOO_FINANCE",
        "sector",
    )
    sec.record_observed_input(
        "Industry", industry, "text",
        "MONTHLY_HUNTER_CACHE" if "industry" in metadata_fallback_fields else "YAHOO_FINANCE",
        "industry",
    )

    concept_names = {
        "Assets", "Cash", "DebtCommercialPaper", "DebtCurrent", "DebtFinanceLease",
        "DebtOtherShortTerm", "DebtShortTermTotal", "DebtTotal", "Equity", "Goodwill",
        "IntangibleAssets", "NetIncome", *SPECIALIZED_CONCEPTS.get(model_key, set()),
    }
    pure_concepts = {"Tier1Ratio", "Tier1WellCapitalizedMinimum"}
    frames = {
        name: sec.fetch_concept(cik, name, units=("pure",) if name in pure_concepts else ("USD",))
        for name in sorted(concept_names)
    }
    shares_frame = sec.fetch_shares_outstanding(cik)
    if stale_required_fact_names(
        {"SharesOutstanding": shares_frame}, decision_timestamp
    ):
        shares_frame = pd.DataFrame()

    def frame(name: str) -> pd.DataFrame:
        return frames.get(name, pd.DataFrame())

    def is_finite(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            return False

    shares_now, shares_source = get_robust_shares(ticker, shares_frame, sec, info)
    share_evidence_ids = GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "SharesOutstanding")
    if shares_source != "SEC" and shares_now > 0:
        share_evidence_ids = [
            sec.record_observed_input(
                "SharesOutstanding_Current", shares_now, "shares_B", shares_source,
                "shares fallback used for specialized market capitalization",
            )
        ]
    reported_market_cap = finite_number(info.get("marketCap"))
    market_cap = (
        price * shares_now
        if shares_now > 0
        else reported_market_cap / 1e9
        if reported_market_cap is not None and reported_market_cap > 0
        else np.nan
    )
    if not math.isfinite(market_cap) or market_cap < MIN_MARKET_CAP_B:
        return ModeCResult(Ticker=ticker, Status=f"Fail: 市值過低或無法取得 {market_cap:.2f}B")
    if shares_now > 0:
        market_cap_evidence_id = sec._record_derived_from_ids(
            "MarketCap", market_cap, "USD_B", "market price * current shares outstanding",
            [price_evidence_id, *share_evidence_ids], "MarketCap:specialized-model-input",
        )
    else:
        market_cap_evidence_id = sec.record_observed_input(
            "MarketCap", market_cap, "USD_B", "YAHOO_FINANCE", "marketCap fallback"
        )

    assets = _specialized_balance(sec, frame("Assets"), "Assets")
    equity = _specialized_balance(sec, frame("Equity"), "Equity")
    average_equity = _specialized_average_balance(sec, frame("Equity"), "Equity")
    goodwill = _specialized_balance(sec, frame("Goodwill"), "Goodwill")
    intangible_assets = _specialized_balance(
        sec, frame("IntangibleAssets"), "IntangibleAssets"
    )
    average_goodwill = (
        _specialized_average_balance(sec, frame("Goodwill"), "Goodwill")
        if not frame("Goodwill").empty
        else np.nan
    )
    average_intangible_assets = (
        _specialized_average_balance(sec, frame("IntangibleAssets"), "IntangibleAssets")
        if not frame("IntangibleAssets").empty
        else np.nan
    )
    tangible_equity = (
        equity - goodwill - intangible_assets
        if all(is_finite(value) for value in (equity, goodwill, intangible_assets))
        else np.nan
    )
    average_tangible_equity = (
        average_equity - average_goodwill - average_intangible_assets
        if all(
            is_finite(value)
            for value in (average_equity, average_goodwill, average_intangible_assets)
        )
        else np.nan
    )
    cash = _specialized_balance(sec, frame("Cash"), "Cash")
    total_debt, debt_method, debt_source_concepts = _specialized_total_debt(sec, frames)
    net_income, _, _ = _specialized_ttm(sec, frame("NetIncome"), "TTM_NetIncome")
    enterprise_value = (
        market_cap + total_debt - cash
        if is_finite(total_debt) and is_finite(cash)
        else np.nan
    )
    enterprise_value_evidence_id = sec._record_derived_from_ids(
        "EnterpriseValue", enterprise_value, "USD_B", "MarketCap + Debt - Cash",
        [
            market_cap_evidence_id,
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtTotal"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtCurrent"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtShortTermTotal"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtOtherShortTerm"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtCommercialPaper"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DebtFinanceLease"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Cash"),
        ],
        "EnterpriseValue:specialized-model-input",
    )
    metrics: Dict[str, Any] = {
        "market_cap_b": market_cap,
        "enterprise_value_b": enterprise_value,
        "assets_b": assets,
        "equity_b": equity,
        "average_equity_b": average_equity,
        "tangible_equity_b": tangible_equity,
        "average_tangible_equity_b": average_tangible_equity,
        "goodwill_b": goodwill,
        "intangible_assets_b": intangible_assets,
        "average_goodwill_b": average_goodwill,
        "average_intangible_assets_b": average_intangible_assets,
        "cash_b": cash,
        "debt_b": total_debt,
        "debt_method": debt_method,
        "debt_source_concepts": debt_source_concepts,
        "net_income_ttm_b": net_income,
    }
    runtime_warnings: List[str] = []
    runtime_optional_missing: List[str] = []
    if model_key in {"BANK", "FINANCIAL_LENDER", "FINANCIAL_FEE"}:
        if frame("Goodwill").empty:
            runtime_optional_missing.append("reported goodwill balance")
        if frame("IntangibleAssets").empty:
            runtime_optional_missing.append("reported intangible-assets balance")

    if model_key in {"BANK", "FINANCIAL_LENDER"}:
        net_interest_income, _, _ = _specialized_ttm(
            sec, frame("NetInterestIncome"), "TTM_NetInterestIncome"
        )
        metrics.update(
            net_interest_income_ttm_b=net_interest_income,
            net_interest_income_growth_pct=_specialized_growth_pct(
                sec, frame("NetInterestIncome"), "NetInterestIncome"
            ),
            loans_b=_specialized_balance(sec, frame("Loans"), "Loans"),
            credit_loss_allowance_b=_specialized_balance(
                sec, frame("CreditLossAllowance"), "CreditLossAllowance"
            ),
        )
        if model_key == "BANK":
            metrics.update(
                deposits_b=_specialized_balance(sec, frame("Deposits"), "Deposits"),
                tier1_ratio_pct=_ratio_as_percent(
                    _specialized_scalar(sec, frame("Tier1Ratio"), "Tier1Ratio")
                ),
                tier1_well_capitalized_min_pct=_ratio_as_percent(
                    _specialized_scalar(
                        sec, frame("Tier1WellCapitalizedMinimum"), "Tier1WellCapitalizedMinimum"
                    )
                ),
                aoci_b=_specialized_balance(sec, frame("AOCI"), "AOCI"),
            )

    if model_key in {"INSURANCE_P_AND_C", "INSURANCE_LIFE"}:
        premiums, _, _ = _specialized_ttm(sec, frame("PremiumsEarned"), "TTM_PremiumsEarned")
        claims, _, _ = _specialized_ttm(
            sec, frame("InsuranceClaims"), "TTM_InsuranceClaims", signed=False
        )
        combined_expense, _, _ = _specialized_ttm(
            sec, frame("InsuranceCombinedExpense"), "TTM_InsuranceCombinedExpense", signed=False
        )
        underwriting, _, _ = _specialized_ttm(
            sec, frame("UnderwritingExpense"), "TTM_UnderwritingExpense", signed=False
        )
        benefits, _, _ = _specialized_ttm(
            sec, frame("PolicyholderBenefits"), "TTM_PolicyholderBenefits", signed=False
        )
        investment_income, _, _ = _specialized_ttm(
            sec, frame("NetInvestmentIncome"), "TTM_NetInvestmentIncome"
        )
        reserve_development, _, _ = _specialized_ttm(
            sec, frame("ReserveDevelopment"), "TTM_ReserveDevelopment"
        )
        metrics.update(
            premiums_earned_ttm_b=premiums,
            claims_incurred_ttm_b=claims,
            combined_expense_ttm_b=combined_expense,
            underwriting_expense_ttm_b=underwriting,
            policyholder_benefits_ttm_b=benefits,
            net_investment_income_ttm_b=investment_income,
            premium_growth_pct=_specialized_growth_pct(
                sec, frame("PremiumsEarned"), "PremiumsEarned"
            ),
            reserve_development_to_premium_pct=safe_div(reserve_development, premiums) * 100.0,
        )

    if model_key == "REIT_EQUITY":
        dna, _, _ = _specialized_ttm(sec, frame("DnA"), "TTM_RealEstateDnA", signed=False)
        interest, _, _ = _specialized_ttm(sec, frame("Interest"), "TTM_Interest", signed=False)
        tax, _, _ = _specialized_ttm(sec, frame("IncomeTaxExpenseBenefit"), "TTM_Tax")
        dividends, _, _ = _specialized_ttm(sec, frame("Dividend"), "TTM_Dividends", signed=False)
        capex, _, _ = _specialized_ttm(
            sec, frame("RealEstateCapEx"), "TTM_RealEstateCapEx", signed=False
        )
        gain, _, _ = _specialized_ttm(sec, frame("GainOnPropertySale"), "TTM_GainOnPropertySale")
        impairment, _, _ = _specialized_ttm(
            sec, frame("RealEstateImpairment"), "TTM_RealEstateImpairment", signed=False
        )
        lease_growth = _specialized_growth_pct(sec, frame("LeaseRevenue"), "LeaseRevenue")
        maintenance = (
            estimate_maintenance_capex_amount(capex, dna, lease_growth / 100.0)
            if is_finite(capex) and is_finite(dna)
            else np.nan
        )
        interest = 0.0 if total_debt <= 0.01 and not is_finite(interest) else interest
        metrics.update(
            real_estate_dna_ttm_b=dna,
            interest_ttm_b=interest,
            tax_ttm_b=tax,
            dividends_ttm_b=dividends,
            maintenance_capex_b=maintenance,
            gain_on_property_sale_ttm_b=gain,
            real_estate_impairment_ttm_b=impairment,
            lease_revenue_growth_pct=lease_growth,
        )

    if model_key == "REIT_MORTGAGE":
        net_interest_income, _, _ = _specialized_ttm(
            sec, frame("NetInterestIncome"), "TTM_NetInterestIncome"
        )
        dividends, _, _ = _specialized_ttm(sec, frame("Dividend"), "TTM_Dividends", signed=False)
        metrics.update(
            dividends_ttm_b=dividends,
            net_interest_income_ttm_b=net_interest_income,
            net_interest_income_growth_pct=_specialized_growth_pct(
                sec, frame("NetInterestIncome"), "NetInterestIncome"
            ),
        )

    if model_key == "REGULATED_UTILITY":
        ebit, _, _ = _specialized_ttm(sec, frame("EBIT"), "TTM_EBIT")
        interest, _, _ = _specialized_ttm(sec, frame("Interest"), "TTM_Interest", signed=False)
        ocf, _, _ = _specialized_ttm(sec, frame("OCF"), "TTM_OCF")
        capex, _, _ = _specialized_ttm(sec, frame("CapEx"), "TTM_CapEx", signed=False)
        dna, _, _ = _specialized_ttm(sec, frame("DnA"), "TTM_DnA", signed=False)
        dividends, _, _ = _specialized_ttm(sec, frame("Dividend"), "TTM_Dividends", signed=False)
        capex_method = "reported cash CapEx"
        if not frame("CapEx").empty:
            latest_capex_end = pd.to_datetime(frame("CapEx")["end"], errors="coerce").max()
            if (
                pd.notna(latest_capex_end)
                and (
                    decision_timestamp.normalize()
                    - pd.Timestamp(latest_capex_end).normalize()
                ).days
                > fact_age_limit_days(frame("CapEx"))
            ):
                capex = np.nan
        if not is_finite(capex) or capex <= 0:
            capex = _specialized_ppe_capex_proxy(sec, frame("PPENet"), dna)
            capex_method = "net PP&E year-over-year change + TTM D&A proxy"
            if is_finite(capex):
                runtime_warnings.append(
                    "reported cash CapEx unavailable; utility CapEx uses a PP&E roll-forward proxy"
                )
                runtime_optional_missing.append("reported utility cash CapEx")
        latest_interest_concept = ""
        if not frame("Interest").empty:
            latest_interest_end = pd.to_datetime(
                frame("Interest")["end"], errors="coerce"
            ).max()
            latest_interest_rows = frame("Interest")[
                pd.to_datetime(frame("Interest")["end"], errors="coerce")
                == latest_interest_end
            ].sort_values("concept_priority")
            if not latest_interest_rows.empty:
                latest_interest_concept = str(
                    latest_interest_rows.iloc[0].get("concept") or ""
                )
        if latest_interest_concept == "InterestPaidNet":
            runtime_warnings.append(
                "accrual interest expense unavailable; ICR uses cash interest paid as a proxy"
            )
            runtime_optional_missing.append("accrual utility interest expense")
        interest = 0.0 if total_debt <= 0.01 and not is_finite(interest) else interest
        metrics.update(
            ebit_ttm_b=ebit,
            interest_ttm_b=interest,
            ocf_ttm_b=ocf,
            capex_ttm_b=capex,
            capex_method=capex_method,
            dividends_ttm_b=dividends,
            ppe_growth_pct=_specialized_balance_growth_pct(sec, frame("PPENet"), "PPENet"),
        )

    if model_key == "CYCLICAL_MIDCYCLE":
        ebit, _, _ = _specialized_ttm(sec, frame("EBIT"), "TTM_EBIT")
        dna, _, _ = _specialized_ttm(sec, frame("DnA"), "TTM_DnA", signed=False)
        interest, _, _ = _specialized_ttm(sec, frame("Interest"), "TTM_Interest", signed=False)
        ocf, _, _ = _specialized_ttm(sec, frame("OCF"), "TTM_OCF")
        capex, _, _ = _specialized_ttm(sec, frame("CapEx"), "TTM_CapEx", signed=False)
        sbc, _, _ = _specialized_ttm(sec, frame("SBC"), "TTM_SBC", signed=False)
        revenue_growth = _specialized_growth_pct(sec, frame("Revenue"), "Revenue")
        maintenance = (
            estimate_maintenance_capex_amount(capex, dna, revenue_growth / 100.0)
            if is_finite(capex) and is_finite(dna)
            else np.nan
        )
        real_fcf = (
            ocf - maintenance - sbc
            if is_finite(ocf) and is_finite(maintenance) and is_finite(sbc)
            else np.nan
        )
        interest = 0.0 if total_debt <= 0.01 and not is_finite(interest) else interest
        metrics.update(
            current_ebitda_b=ebit + dna,
            ebitda_history_b=_specialized_ebitda_history(sec, frame("EBIT"), frame("DnA")),
            interest_ttm_b=interest,
            dna_ttm_b=dna,
            ocf_ttm_b=ocf,
            capex_ttm_b=capex,
            maintenance_capex_b=maintenance,
            real_fcf_ttm_b=real_fcf,
            revenue_growth_pct=revenue_growth,
        )

    if model_key == "FINANCIAL_FEE":
        revenue, _, _ = _specialized_ttm(sec, frame("Revenue"), "TTM_Revenue")
        ebit, _, _ = _specialized_ttm(sec, frame("EBIT"), "TTM_EBIT")
        ocf, _, _ = _specialized_ttm(sec, frame("OCF"), "TTM_OCF")
        dna, _, _ = _specialized_ttm(sec, frame("DnA"), "TTM_DnA", signed=False)
        metrics.update(
            revenue_ttm_b=revenue,
            ebit_ttm_b=ebit,
            ocf_ttm_b=ocf,
            ebitda_ttm_b=ebit + dna,
            revenue_growth_pct=_specialized_growth_pct(sec, frame("Revenue"), "Revenue"),
            loans_b=_specialized_balance(sec, frame("Loans"), "Loans"),
            credit_loss_allowance_b=_specialized_balance(
                sec, frame("CreditLossAllowance"), "CreditLossAllowance"
            ),
            net_interest_income_growth_pct=_specialized_growth_pct(
                sec, frame("NetInterestIncome"), "NetInterestIncome"
            ),
        )

    auto_lender = bool(
        model_key == "FINANCIAL_FEE"
        and is_finite(metrics.get("loans_b"))
        and is_finite(metrics.get("assets_b"))
        and float(metrics["assets_b"]) > 0
        and float(metrics["loans_b"]) / float(metrics["assets_b"]) >= 0.20
        and is_finite(metrics.get("credit_loss_allowance_b"))
    )
    if auto_lender:
        model_key = "FINANCIAL_LENDER"
        model_route = {
            **model_route,
            "reason": (
                "SEC balance-sheet refinement from FINANCIAL_FEE: "
                "loans/assets >=20% with a reported credit-loss allowance"
            ),
        }
    stale_inputs: List[str] = []

    def apply_recency_gate(recency_model_key: str) -> None:
        for metric_name, concept_group in SPECIALIZED_RECENCY_CONCEPTS.get(recency_model_key, {}).items():
            if not is_finite(metrics.get(metric_name)):
                continue
            if metric_name == "debt_b" and metrics.get("debt_source_concepts"):
                concept_group = tuple(metrics["debt_source_concepts"])
            if metric_name == "capex_ttm_b":
                concept_group = (
                    ("PPENet", "DnA")
                    if str(metrics.get("capex_method") or "").startswith("net PP&E")
                    else ("CapEx",)
                )
            stale_sources = []
            for concept_name in concept_group:
                source_frame = frame(concept_name)
                if source_frame.empty or "end" not in source_frame.columns:
                    continue
                latest_end = pd.to_datetime(source_frame["end"], errors="coerce").max()
                if pd.notna(latest_end):
                    age_days = (
                        decision_timestamp.normalize()
                        - pd.Timestamp(latest_end).normalize()
                    ).days
                    age_limit = fact_age_limit_days(source_frame)
                    if age_days > age_limit:
                        stale_sources.append(
                            f"{concept_name} is {age_days} days old (limit {age_limit})"
                        )
            if not stale_sources:
                continue
            metrics[metric_name] = np.nan
            stale_inputs.append(f"{metric_name}: " + ", ".join(stale_sources))

    apply_recency_gate(model_key)
    evaluation = evaluate_industry_model(model_key, metrics)
    if auto_lender:
        evaluation.warnings.append(
            "Financial specialty was classified as a lender because loans are at least 20% of assets"
        )
    evaluation.warnings.extend(runtime_warnings)
    extra_missing = list(SPECIALIZED_DISCLOSURE_GAPS.get(model_key, []))
    extra_missing.extend(runtime_optional_missing)
    extra_missing.extend(stale_inputs)
    if shares_source != "SEC":
        extra_missing.append("SEC current shares outstanding")
    evidence_stats = GLOBAL_EVIDENCE_LEDGER.selected_source_stats(ticker)
    confidence = assess_specialized_data_confidence(evidence_stats, evaluation, extra_missing)

    source_evidence_ids = GLOBAL_EVIDENCE_LEDGER.selected_source_evidence_ids(ticker)
    model_lineage_ids = [market_cap_evidence_id, enterprise_value_evidence_id, *source_evidence_ids]
    metric_evidence_ids = []
    for metric_name, metric_value in evaluation.metrics.items():
        if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float, np.number)):
            continue
        unit = "USD_B" if metric_name.endswith("_b") else "percent" if metric_name.endswith(("_pct", "_pp")) else "x" if metric_name.endswith("_x") else "number"
        metric_evidence_ids.append(
            sec._record_derived_from_ids(
                f"{model_key}:{metric_name}", metric_value, unit,
                f"specialized {model_key} calculation defined in mode_c_industry_models.py",
                model_lineage_ids, f"{model_key}:{metric_name}:model-output",
            )
        )
    component_evidence_ids = [
        sec._record_derived_from_ids(
            f"{model_key}:component:{name}", score, "score_0_100",
            f"specialized {model_key} component score", metric_evidence_ids,
            f"{model_key}:{name}:component-score",
        )
        for name, score in evaluation.components.items()
    ]
    industry_score_evidence_id = sec._record_derived_from_ids(
        f"{model_key}:Industry_Model_Score", evaluation.score, "score_0_100",
        "coverage-adjusted weighted specialized industry score", component_evidence_ids,
        f"{model_key}:final-score",
    )
    confidence_evidence_id = sec._record_derived_from_ids(
        f"{model_key}:Data_Confidence_Score", confidence["score"], "score_0_100",
        "point-in-time evidence coverage and specialized disclosure completeness gate",
        [*source_evidence_ids, industry_score_evidence_id], f"{model_key}:confidence-gate",
    )

    if evaluation.hard_failures:
        decision_state = "FAIL"
        status = f"Fail: {evaluation.hard_failures[0]}"
    elif evaluation.decision == "ABSTAIN" or bool(confidence["abstain"]):
        decision_state = "ABSTAIN"
        status = (
            f"Abstain: {model_key} required evidence incomplete"
            if evaluation.decision == "ABSTAIN"
            else f"Abstain: {model_key} data confidence {float(confidence['score']):.0f}"
        )
    elif evaluation.decision == "FAIL":
        decision_state = "FAIL"
        reason = evaluation.hard_failures[0] if evaluation.hard_failures else f"specialized score {evaluation.score:.1f}<60"
        status = f"Fail: {reason}"
    else:
        decision_state, status = "PASS", "Pass"
    eligible = bool(
        decision_state == "PASS" and is_finite(evaluation.score)
        and evaluation.score >= MIN_LONG_TERM_SCORE
        and float(confidence["score"]) >= MIN_DATA_CONFIDENCE
    )
    if eligible and evaluation.score >= HIGH_PRIORITY_SCORE:
        verdict = "高優先研究：專用產業模型、資料信心與風險閘門達標"
        action, starter_weight = "完成專用產業人工覆核後，可考慮 1.5% 總資產起始部位", STARTER_WEIGHT_PCT_TOTAL
    elif eligible and evaluation.score >= SMALL_POSITION_SCORE:
        verdict = "可考慮小部位：專用模型達標，仍須完成產業揭露覆核"
        action, starter_weight = "完成專用產業人工覆核後，可考慮 1.0% 總資產起始部位", STARTER_WEIGHT_MIN_PCT_TOTAL
    elif eligible:
        verdict = "專用產業研究候選：尚未達買入分數"
        action, starter_weight = "列入觀察，不建立部位；等待分數達 75 且非標準揭露完成覆核", 0.0
    elif decision_state == "ABSTAIN":
        verdict = "暫不判斷：專用模型關鍵證據或資料信心不足"
        action, starter_weight = "不得建立部位；先補齊監管、產業或公司自訂揭露", 0.0
    else:
        verdict = "排除：專用產業風險閘門或最低分數未通過"
        action, starter_weight = "不進入主動投資研究池", 0.0
    sec._record_derived_from_ids(
        f"{model_key}:Long_Term_Eligible", eligible, "boolean",
        "specialized model pass, score >= 60 and data confidence >= 70",
        [industry_score_evidence_id, confidence_evidence_id], f"{model_key}:eligibility",
    )

    result_metrics = evaluation.metrics
    interest_coverage = result_metrics.get("interest_coverage_x", result_metrics.get("trough_interest_coverage_x", np.nan))
    ebitda_value = result_metrics.get("current_ebitda_b", result_metrics.get("ebitda_ttm_b"))
    try:
        specialized_icr = float(interest_coverage)
    except (TypeError, ValueError, OverflowError):
        specialized_icr = np.nan
    if not (math.isfinite(specialized_icr) or math.isinf(specialized_icr)):
        specialized_icr = np.nan
    specialized_icr_method = str(
        result_metrics.get("debt_service_method")
        or f"{model_key} specialized coverage"
    )
    return ModeCResult(
        Ticker=ticker,
        Status=status,
        Scoring_Framework=f"INDUSTRY_SPECIALIZED_{model_key}_V1",
        Price=round(price, 2),
        Sector=sector,
        Industry=industry,
        Model_Route=str(model_route.get("route") or ""),
        Model_Supported=True,
        Model_Route_Reason=str(model_route.get("reason") or ""),
        Industry_Model_Key=model_key,
        Industry_Model_Decision=evaluation.decision,
        Industry_Model_Score=round(evaluation.score, 2) if is_finite(evaluation.score) else np.nan,
        Industry_Model_Coverage=round(evaluation.score_coverage * 100.0, 1),
        Industry_Model_Metrics_JSON=json.dumps(_specialized_json(evaluation.metrics), ensure_ascii=False, sort_keys=True),
        Industry_Model_Components_JSON=json.dumps(_specialized_json(evaluation.components), ensure_ascii=False, sort_keys=True),
        Industry_Model_Warnings="; ".join(evaluation.warnings),
        Industry_Model_Hard_Failures="; ".join(evaluation.hard_failures),
        Industry_Model_Required_Missing="; ".join(evaluation.required_missing),
        Industry_Model_Optional_Missing="; ".join(evaluation.optional_missing),
        Decision_State=decision_state,
        Decision_Timestamp=decision_timestamp.isoformat(),
        Data_Confidence_Score=float(confidence["score"]),
        Data_Confidence_Reasons="; ".join(str(item) for item in confidence["reasons"]),
        Evidence_Source_Count=float(evidence_stats["selected_source_count"]),
        Evidence_AcceptedAt_Ratio=round(float(evidence_stats["accepted_at_ratio"]), 3),
        MarketCap_B=round(market_cap, 3),
        EV_B=round(enterprise_value, 3),
        Total_Debt_B=round(total_debt, 3) if is_finite(total_debt) else np.nan,
        Cash_B=round(cash, 3) if is_finite(cash) else np.nan,
        Net_Debt_B=(
            round(total_debt - cash, 3)
            if is_finite(total_debt) and is_finite(cash)
            else np.nan
        ),
        Debt_Source_Method=debt_method,
        TTM_OCF_B=round(float(result_metrics.get("ocf_ttm_b")), 3) if is_finite(result_metrics.get("ocf_ttm_b")) else np.nan,
        Dynamic_CapEx_B=round(float(result_metrics.get("capex_ttm_b")), 3) if is_finite(result_metrics.get("capex_ttm_b")) else np.nan,
        Maintenance_CapEx_B=round(float(result_metrics.get("maintenance_capex_b")), 3) if is_finite(result_metrics.get("maintenance_capex_b")) else np.nan,
        Industry_Stress_Extension_Status="NOT_IMPLEMENTED",
        Industry_Stress_Extension_Reason=(
            f"{model_key} requires a dedicated industry stress extension; generic EBITDA shock is not displayed"
        ),
        EBITDA_B=round(float(ebitda_value), 3) if is_finite(ebitda_value) else np.nan,
        ICR=(round(specialized_icr, 2) if math.isfinite(specialized_icr) else specialized_icr),
        ICR_Method=specialized_icr_method,
        Long_Term_Score=(
            round(evaluation.score, 2)
            if decision_state != "ABSTAIN" and is_finite(evaluation.score)
            else np.nan
        ),
        Long_Term_Eligible=eligible,
        Research_Action=action,
        Suggested_Starter_Weight_pct_Total=starter_weight,
        Physical_Check=f"使用 {model_key} 專用產業模型；非標準揭露必須人工覆核。",
        Data_Quality_Flags="; ".join([*evaluation.warnings, *evaluation.hard_failures, *extra_missing]) or "OK",
        Verdict=verdict,
        Trade_Tool=action,
        Agent_Tasks=list(SPECIALIZED_AGENT_TASKS.get(model_key, [])),
    )




def _run_mode_c_pipeline_core(
    ticker: str,
    cik: str,
    email: str,
    decision_timestamp: Optional[pd.Timestamp] = None,
) -> ModeCResult:
    try:
        decision_timestamp = SECDataDistiller._utc_naive_timestamp(
            decision_timestamp
            if decision_timestamp is not None
            else pd.Timestamp.now(tz="UTC")
        )
        pm = fetch_price_metrics(ticker)
        if not pm:
            return ModeCResult(Ticker=ticker, Status="Fail: 無價格資料")
        if pm["dollar_volume"] < MIN_LIQUIDITY_USD:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 流動性不足 ${pm['dollar_volume']/1e6:.1f}M")


        info = safe_yf_info(ticker)
        rejection_reason = common_equity_rejection_reason(ticker, info)
        if rejection_reason:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 名單驗證 ({rejection_reason})")
        sector = str(info.get("sector") or "").strip()
        industry = str(info.get("industry") or "").strip()
        model_route = route_industry_model(sector, industry)
        if not bool(model_route["supported"]):
            return ModeCResult(
                Ticker=ticker,
                Status=f"Abstain: {model_route['route']} 模型路由不可判定",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Supported=False,
                Model_Route_Reason=str(model_route["reason"]),
                Industry_Model_Key=str(model_route.get("model_key") or "UNKNOWN"),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons="Sector/industry metadata cannot select a supported accounting model",
            )
        model_key = str(model_route.get("model_key") or "GENERAL_CORPORATE")
        if model_key in SPECIALIZED_MODEL_KEYS:
            return run_specialized_mode_c_pipeline(
                ticker, cik, email, pm, info, model_route, decision_timestamp
            )
        if model_key != "GENERAL_CORPORATE":
            return ModeCResult(
                Ticker=ticker,
                Status=f"Abstain: 未註冊模型 {model_key}",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Supported=False,
                Model_Route_Reason=str(model_route["reason"]),
                Industry_Model_Key=model_key,
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons="No evaluator is registered for the routed model key",
            )
        price = first_finite_positive(
            info.get("currentPrice"),
            info.get("regularMarketPrice"),
            pm.get("last_close"),
        )
        if not math.isfinite(price) or price <= 0:
            return ModeCResult(Ticker=ticker, Status="Fail: 價格失真")


        sec = SECDataDistiller(
            email,
            ticker=ticker,
            cik=cik,
            decision_timestamp=decision_timestamp,
        )
        price_evidence_id = sec.record_observed_input(
            "MarketPrice",
            price,
            "USD_per_share",
            "YAHOO_FINANCE",
            "Close/currentPrice",
        )
        sec.record_observed_input(
            "DollarVolume30D",
            pm["dollar_volume"],
            "USD",
            "YAHOO_FINANCE",
            "mean(Close*Volume,30D)",
        )
        momentum_evidence_id = ""
        if pm.get("momentum_12m") is not None:
            momentum_evidence_id = sec.record_observed_input(
                "Momentum12M",
                pm["momentum_12m"],
                "percent",
                "YAHOO_FINANCE",
                "12M price momentum excluding latest month",
            )
        metadata_fallback_fields = set(
            info.get("_verifiedUniverseMetadataFallbackFields") or []
        )
        sec.record_observed_input(
            "Sector", sector, "text",
            "MONTHLY_HUNTER_CACHE" if "sector" in metadata_fallback_fields else "YAHOO_FINANCE",
            "sector",
        )
        sec.record_observed_input(
            "Industry", industry, "text",
            "MONTHLY_HUNTER_CACHE" if "industry" in metadata_fallback_fields else "YAHOO_FINANCE",
            "industry",
        )
        df_ocf = sec.fetch_concept(cik, "OCF")
        df_capex = sec.fetch_concept(cik, "CapEx")
        df_sbc = sec.fetch_concept(cik, "SBC")
        df_ebit = sec.fetch_concept(cik, "EBIT")
        df_int = sec.fetch_concept(cik, "Interest")
        df_dna = sec.fetch_concept(cik, "DnA")
        df_rev = sec.fetch_concept(cik, "Revenue")
        df_gp = sec.fetch_concept(cik, "GrossProfit")
        df_cogs = sec.fetch_concept(cik, "COGS")
        df_inv = sec.fetch_concept(cik, "Inventory")
        df_debt_total = sec.fetch_concept(cik, "DebtTotal")
        df_debt_current = sec.fetch_concept(cik, "DebtCurrent")
        df_debt_short_total = sec.fetch_concept(cik, "DebtShortTermTotal")
        df_debt_other_short = sec.fetch_concept(cik, "DebtOtherShortTerm")
        df_debt_commercial_paper = sec.fetch_concept(cik, "DebtCommercialPaper")
        df_debt_finance_lease = sec.fetch_concept(cik, "DebtFinanceLease")
        df_cash = sec.fetch_concept(cik, "Cash")
        df_equity = sec.fetch_concept(cik, "Equity")
        df_net_income = sec.fetch_concept(cik, "NetIncome")
        df_buyback = sec.fetch_concept(cik, "Buyback")
        df_issuance = sec.fetch_concept(cik, "StockIssuance")
        df_shares = sec.fetch_shares_outstanding(cik)
        if stale_required_fact_names(
            {"SharesOutstanding": df_shares}, decision_timestamp
        ):
            df_shares = pd.DataFrame()
        df_tax = sec.fetch_concept(cik, "IncomeTaxExpenseBenefit") # 補齊：強制抓取稅務標籤以完成勾稽


        required_facts = {
            "OCF": df_ocf, "Revenue": df_rev, "EBIT": df_ebit, "CapEx": df_capex,
            "D&A": df_dna, "GrossProfit": df_gp, "NetIncome": df_net_income,
            "Cash": df_cash, "Equity": df_equity,
        }
        missing_facts = [name for name, frame in required_facts.items() if frame.empty]
        if missing_facts:
            return ModeCResult(
                Ticker=ticker,
                Status=f"Abstain: SEC 核心資料缺失 {'/'.join(missing_facts)}",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons=f"Missing critical SEC metrics: {'/'.join(missing_facts)}",
            )
        stale_facts = stale_required_fact_names(required_facts, decision_timestamp)
        if stale_facts:
            return ModeCResult(
                Ticker=ticker,
                Status=f"Abstain: SEC 核心資料過舊 {'/'.join(stale_facts)}",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons=(
                    "Critical SEC periods exceed the 240-day domestic or 550-day foreign annual limit: "
                    + ", ".join(stale_facts)
                ),
            )


        ocf_ttm, ocf_method, ocf_evidence = sec.ttm_flow(df_ocf, normalized_metric="TTM_OCF")
        capex_ttm, capex_method, capex_evidence = sec.ttm_flow(df_capex, signed=False, normalized_metric="TTM_CapEx")
        sbc_ttm, sbc_method, sbc_evidence = sec.ttm_flow(df_sbc, signed=False, normalized_metric="TTM_SBC")
        ebit_ttm, ebit_method, ebit_evidence = sec.ttm_flow(df_ebit, normalized_metric="TTM_EBIT")
        dna_ttm, dna_method, dna_evidence = sec.ttm_flow(df_dna, signed=False, normalized_metric="TTM_DnA")
        rev_ttm, rev_method, rev_evidence = sec.ttm_flow(df_rev, normalized_metric="TTM_Revenue")
        interest_ttm, interest_method, interest_evidence = sec.ttm_flow(df_int, signed=False, normalized_metric="TTM_Interest")
        interest_source_tags = GLOBAL_EVIDENCE_LEDGER.source_original_tags(
            str(interest_evidence.get("evidence_id") or "")
        )
        interest_cash_proxy_available = "InterestPaidNet" in interest_source_tags
        net_income_ttm, net_income_method, net_income_evidence = sec.ttm_flow(df_net_income, normalized_metric="TTM_NetIncome")
        buyback_ttm, _, buyback_evidence = sec.ttm_flow(df_buyback, signed=False, normalized_metric="TTM_Buyback")
        issuance_ttm, _, issuance_evidence = sec.ttm_flow(df_issuance, signed=False, normalized_metric="TTM_StockIssuance")
        tax_ttm, tax_method, tax_evidence = sec.ttm_flow(df_tax, normalized_metric="TTM_Tax") if not df_tax.empty else (np.nan, "missing", {})
        fcf_stability = calculate_fcf_stability(
            sec, df_ocf, df_capex, df_sbc, df_dna, df_rev, df_net_income
        )

        critical_ttm = {
            "OCF": (ocf_ttm, ocf_evidence),
            "CapEx": (capex_ttm, capex_evidence),
            "EBIT": (ebit_ttm, ebit_evidence),
            "D&A": (dna_ttm, dna_evidence),
            "Revenue": (rev_ttm, rev_evidence),
            "NetIncome": (net_income_ttm, net_income_evidence),
        }
        missing_ttm = [
            name
            for name, (value, evidence) in critical_ttm.items()
            if not math.isfinite(value) or not str(evidence.get("evidence_id") or "")
        ]
        if missing_ttm:
            return ModeCResult(
                Ticker=ticker,
                Status=f"Abstain: TTM 證據鏈不完整 {'/'.join(missing_ttm)}",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons=(
                    "Critical TTM metrics require a finite value and selected source lineage: "
                    + ", ".join(missing_ttm)
                ),
            )
        if capex_ttm < 0:
            return ModeCResult(
                Ticker=ticker,
                Status="Abstain: CapEx TTM 符號無效",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons="Absolute CapEx cannot be negative",
            )
        sbc_external_fallback = False
        sbc_metric_evidence_id = str(sbc_evidence.get("evidence_id") or "")
        if not sbc_metric_evidence_id:
            reported_sbc = finite_number(info.get("shareBasedCompensation"))
            if reported_sbc is not None:
                sbc_ttm = abs(reported_sbc) / 1e9
                sbc_external_fallback = True
                sbc_metric_evidence_id = sec.record_observed_input(
                    "TTM_SBC",
                    sbc_ttm,
                    "USD_B",
                    "YAHOO_FINANCE",
                    "shareBasedCompensation fallback",
                )
        if not sbc_metric_evidence_id:
            return ModeCResult(
                Ticker=ticker,
                Status="Abstain: SBC TTM 證據缺失",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Industry_Model_Key=model_key,
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons=(
                    "Missing SEC or explicit market-data SBC evidence cannot be treated as zero"
                ),
            )


        shares_sec_now, shares_1y_ago, shares_3y_ago = sec.get_shares_now_1y_3y(df_shares)
        per_share_growth = calculate_per_share_growth_3y(
            sec,
            df_ocf,
            df_capex,
            df_sbc,
            df_dna,
            df_rev,
            df_net_income,
            df_shares,
        )
        shares_now, shares_source = get_robust_shares(ticker, df_shares, sec, info)
        if shares_source == "SEC":
            latest_share_fact = sec._instant_facts(df_shares).tail(1)
            share_evidence_ids = (
                [str(item) for item in latest_share_fact["evidence_id"].dropna().tolist()]
                if "evidence_id" in latest_share_fact.columns
                else []
            )
        elif shares_now > 0:
            share_evidence_id = sec.record_observed_input(
                "SharesOutstanding_Current",
                shares_now,
                "shares_B",
                shares_source,
                "shares fallback used for market capitalization",
            )
            share_evidence_ids = [share_evidence_id]
        reported_market_cap = finite_number(info.get("marketCap"))
        mcap = (
            price * shares_now
            if shares_now > 0
            else reported_market_cap / 1e9
            if reported_market_cap is not None and reported_market_cap > 0
            else np.nan
        )
        if not math.isfinite(mcap) or mcap < MIN_MARKET_CAP_B:
            return ModeCResult(Ticker=ticker, Status=f"Fail: 市值過低或無法取得 {mcap:.2f}B")
        if shares_now > 0:
            market_cap_evidence_id = sec._record_derived_from_ids(
                "MarketCap",
                mcap,
                "USD_B",
                "market price * current shares outstanding",
                [price_evidence_id, *share_evidence_ids],
                "MarketCap:model-input",
            )
        else:
            market_cap_evidence_id = sec.record_observed_input(
                "MarketCap",
                mcap,
                "USD_B",
                "YAHOO_FINANCE",
                "marketCap fallback",
            )

        debt_frames = {
            "DebtTotal": df_debt_total,
            "DebtCurrent": df_debt_current,
            "DebtShortTermTotal": df_debt_short_total,
            "DebtOtherShortTerm": df_debt_other_short,
            "DebtCommercialPaper": df_debt_commercial_paper,
            "DebtFinanceLease": df_debt_finance_lease,
        }
        total_debt, debt_method, debt_source_concepts = _specialized_total_debt(
            sec, debt_frames
        )
        debt_sec_evidence_available = math.isfinite(total_debt)
        debt_external_fallback = False
        if not debt_sec_evidence_available:
            try:
                yahoo_total_debt = float(info.get("totalDebt")) / 1e9
            except (TypeError, ValueError, OverflowError):
                yahoo_total_debt = np.nan
            if not math.isfinite(yahoo_total_debt) or yahoo_total_debt < 0:
                return ModeCResult(
                    Ticker=ticker,
                    Status="Abstain: SEC 與市場資料皆無法確認總負債",
                    Sector=sector,
                    Industry=industry,
                    Model_Route=str(model_route["route"]),
                    Model_Route_Reason=str(model_route["reason"]),
                    Decision_State="ABSTAIN",
                    Decision_Timestamp=decision_timestamp.isoformat(),
                    Data_Confidence_Score=0.0,
                    Data_Confidence_Reasons="Unknown debt cannot be treated as zero",
                )
            total_debt = yahoo_total_debt
            debt_method = "Yahoo totalDebt current fallback"
            debt_source_concepts = ["DebtTotal"]
            debt_external_fallback = True
            sec.record_observed_input(
                "DebtTotal",
                total_debt,
                "USD_B",
                "YAHOO_FINANCE",
                "totalDebt fallback",
            )
        cash = sec.latest_balance(df_cash, "Cash")
        equity = sec.latest_balance(df_equity, "Equity")
        if not math.isfinite(cash) or not math.isfinite(equity):
            return ModeCResult(
                Ticker=ticker,
                Status="Abstain: 現金或股東權益缺少可用即時點證據",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons="Cash and equity must be finite point-in-time balance-sheet facts",
            )
        ev = mcap + total_debt - cash
        debt_evidence_ids = []
        for debt_metric in debt_source_concepts:
            debt_evidence_ids.extend(
                GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, debt_metric)
            )
        debt_evidence_ids = list(dict.fromkeys(debt_evidence_ids))
        cash_evidence_ids = GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Cash")
        ev_evidence_id = sec._record_derived_from_ids(
            "EnterpriseValue",
            ev,
            "USD_B",
            "MarketCap + TotalDebt - Cash",
            [market_cap_evidence_id, *debt_evidence_ids, *cash_evidence_ids],
            "EnterpriseValue:model-input",
        )
        if ev <= 0:
            return ModeCResult(
                Ticker=ticker,
                Status="Abstain: Enterprise Value 非正，需使用淨現金特殊情境模型",
                Price=round(price, 2),
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons=(
                    "Non-positive enterprise value makes EV yield, EV/EBITDA and reverse valuation non-comparable"
                ),
                MarketCap_B=round(mcap, 3),
                EV_B=round(ev, 3),
                Total_Debt_B=round(total_debt, 3),
                Cash_B=round(cash, 3),
                Net_Debt_B=round(total_debt - cash, 3),
                Debt_Source_Method=debt_method,
                ICR_Method="not_evaluated_nonpositive_ev",
            )
        pretax_proxy = net_income_ttm + tax_ttm
        reported_tax_rate_usable = bool(
            pretax_proxy > 0
            and tax_ttm >= 0
            and bool(tax_evidence.get("evidence_id"))
        )
        effective_tax_rate = (
            max(0.0, min(0.35, safe_div(tax_ttm, pretax_proxy, 0.21)))
            if reported_tax_rate_usable
            else 0.21
        )
        if reported_tax_rate_usable:
            effective_tax_rate_evidence_id = sec._record_derived_from_ids(
                "Effective_Tax_Rate",
                effective_tax_rate,
                "ratio",
                "TTM Tax / (TTM Net Income + TTM Tax), bounded to 0%-35%",
                [
                    str(tax_evidence.get("evidence_id") or ""),
                    str(net_income_evidence.get("evidence_id") or ""),
                ],
                "Effective_Tax_Rate:model-input",
            )
        else:
            effective_tax_rate_evidence_id = sec.record_observed_input(
                "Effective_Tax_Rate_Assumption",
                effective_tax_rate,
                "ratio",
                "MODEL_ASSUMPTION",
                "21% fallback when reported TTM tax rate is unavailable or distorted by a tax benefit",
            )
        invested_capital = equity + total_debt - cash
        roic = safe_div(ebit_ttm * (1.0 - effective_tax_rate), invested_capital) * 100 if invested_capital > 0 else np.nan
        roce = safe_div(ebit_ttm, invested_capital) * 100 if invested_capital > 0 else np.nan
        roic_evidence_id = sec._record_derived_from_ids(
            "ROIC",
            roic,
            "percent",
            "TTM EBIT * (1 - effective tax rate) / (Equity + Debt - Cash)",
            [
                str(ebit_evidence.get("evidence_id") or ""),
                effective_tax_rate_evidence_id,
                *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Equity"),
                *debt_evidence_ids,
                *cash_evidence_ids,
            ],
            "ROIC:model-input",
        )
        sec._record_derived_from_ids(
            "ROCE",
            roce,
            "percent",
            "TTM EBIT / (Equity + Debt - Cash)",
            [
                str(ebit_evidence.get("evidence_id") or ""),
                *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Equity"),
                *debt_evidence_ids,
                *cash_evidence_ids,
            ],
            "ROCE:model-input",
        )

        dynamic_capex = abs(capex_ttm)
        capex_profile = estimate_maintenance_capex_profile(
            sec, df_capex, df_dna, df_rev, dynamic_capex, dna_ttm
        )
        maintenance_capex = float(capex_profile["maintenance_capex_b"])
        maintenance_capex_low = float(capex_profile["maintenance_capex_low_b"])
        maintenance_capex_high = float(capex_profile["maintenance_capex_high_b"])
        growth_capex = float(capex_profile["growth_capex_b"])
        conservative_real_fcf = ocf_ttm - dynamic_capex - sbc_ttm
        real_fcf = ocf_ttm - maintenance_capex - sbc_ttm
        real_fcf_low = ocf_ttm - maintenance_capex_high - sbc_ttm
        real_fcf_high = ocf_ttm - maintenance_capex_low - sbc_ttm
        real_fcf_yield = safe_div(real_fcf, mcap) * 100
        conservative_real_fcf_yield = safe_div(conservative_real_fcf, mcap) * 100
        real_fcf_to_ev_yield = safe_div(real_fcf, ev) * 100
        conservative_real_fcf_to_ev_yield = safe_div(conservative_real_fcf, ev) * 100
        real_fcf_yield_low = safe_div(real_fcf_low, mcap) * 100
        real_fcf_yield_high = safe_div(real_fcf_high, mcap) * 100
        maintenance_source_ids = [
            str(payload.get("evidence_id") or "")
            for payload in [capex_evidence, dna_evidence, rev_evidence]
        ]
        maintenance_capex_evidence_id = sec._record_derived_from_ids(
            "Maintenance_CapEx",
            maintenance_capex,
            "USD_B",
            "D&A anchor plus revenue-growth classification of CapEx excess",
            maintenance_source_ids,
            "Maintenance_CapEx:model-input",
        )
        maintenance_capex_low_evidence_id = sec._record_derived_from_ids(
            "Maintenance_CapEx_Low",
            maintenance_capex_low,
            "USD_B",
            "low maintenance scenario: D&A anchor plus lower excess-CapEx allocation",
            maintenance_source_ids,
            "Maintenance_CapEx_Low:model-input",
        )
        maintenance_capex_high_evidence_id = sec._record_derived_from_ids(
            "Maintenance_CapEx_High",
            maintenance_capex_high,
            "USD_B",
            "high maintenance scenario: D&A anchor plus higher excess-CapEx allocation",
            maintenance_source_ids,
            "Maintenance_CapEx_High:model-input",
        )
        growth_capex_evidence_id = sec._record_derived_from_ids(
            "Growth_CapEx",
            growth_capex,
            "USD_B",
            "TTM CapEx - estimated Maintenance CapEx",
            [str(capex_evidence.get("evidence_id") or ""), maintenance_capex_evidence_id],
            "Growth_CapEx:model-input",
        )
        conservative_fcf_evidence_id = sec._record_derived_from_ids(
            "Conservative_Real_FCF",
            conservative_real_fcf,
            "USD_B",
            "TTM OCF - all TTM CapEx - TTM SBC",
            [
                str(ocf_evidence.get("evidence_id") or ""),
                str(capex_evidence.get("evidence_id") or ""),
                sbc_metric_evidence_id,
            ],
            "Conservative_Real_FCF:model-input",
        )
        real_fcf_evidence_id = sec._record_derived_from_ids(
            "Real_FCF",
            real_fcf,
            "USD_B",
            "TTM OCF - Maintenance CapEx - TTM SBC",
            [
                str(ocf_evidence.get("evidence_id") or ""),
                maintenance_capex_evidence_id,
                sbc_metric_evidence_id,
            ],
            "Real_FCF:model-input",
        )
        ebitda = ebit_ttm + dna_ttm
        if ebitda <= 0:
            return ModeCResult(Ticker=ticker, Status="Fail: EBITDA 非正值")
        ev_ebitda = safe_div(ev, ebitda)
        ebitda_evidence_id = sec._record_derived_from_ids(
            "TTM_EBITDA",
            ebitda,
            "USD_B",
            "TTM EBIT + TTM D&A",
            [
                str(ebit_evidence.get("evidence_id") or ""),
                str(dna_evidence.get("evidence_id") or ""),
            ],
            "TTM_EBITDA:model-input",
        )
        ev_ebitda_evidence_id = sec._record_derived_from_ids(
            "EV_EBITDA",
            ev_ebitda,
            "x",
            "Enterprise Value / TTM EBITDA",
            [ev_evidence_id, ebitda_evidence_id],
            "EV_EBITDA:model-input",
        )
        real_fcf_yield_evidence_id = sec._record_derived_from_ids(
            "Real_FCF_to_MarketCap_Yield",
            real_fcf_yield,
            "percent",
            "Maintenance Real FCF / Market Capitalization",
            [real_fcf_evidence_id, market_cap_evidence_id],
            "Real_FCF_to_MarketCap_Yield:model-input",
        )
        pe = safe_div(mcap, net_income_ttm)
        pe_evidence_id = sec._record_derived_from_ids(
            "PE",
            pe,
            "x",
            "MarketCap / TTM Net Income",
            [market_cap_evidence_id, str(net_income_evidence.get("evidence_id") or "")],
            "PE:model-input",
        )
        sec._record_derived_from_ids(
            "Conservative_Real_FCF_to_MarketCap_Yield",
            conservative_real_fcf_yield,
            "percent",
            "Conservative Real FCF / Market Capitalization",
            [conservative_fcf_evidence_id, market_cap_evidence_id],
            "Conservative_Real_FCF_to_MarketCap_Yield:model-input",
        )
        sec._record_derived_from_ids(
            "Real_FCF_to_EV_Yield",
            real_fcf_to_ev_yield,
            "percent",
            "Maintenance Real FCF / Enterprise Value",
            [real_fcf_evidence_id, ev_evidence_id],
            "Real_FCF_to_EV_Yield:model-input",
        )
        sec._record_derived_from_ids(
            "Conservative_Real_FCF_to_EV_Yield",
            conservative_real_fcf_to_ev_yield,
            "percent",
            "Conservative Real FCF / Enterprise Value",
            [conservative_fcf_evidence_id, ev_evidence_id],
            "Conservative_Real_FCF_to_EV_Yield:model-input",
        )
        sec._record_derived_from_ids(
            "Maintenance_Real_FCF_Yield_Low",
            real_fcf_yield_low,
            "percent",
            "(TTM OCF - high Maintenance CapEx - TTM SBC) / Market Capitalization",
            [str(ocf_evidence.get("evidence_id") or ""), maintenance_capex_high_evidence_id, sbc_metric_evidence_id, market_cap_evidence_id],
            "Maintenance_Real_FCF_Yield_Low:model-input",
        )
        sec._record_derived_from_ids(
            "Maintenance_Real_FCF_Yield_High",
            real_fcf_yield_high,
            "percent",
            "(TTM OCF - low Maintenance CapEx - TTM SBC) / Market Capitalization",
            [str(ocf_evidence.get("evidence_id") or ""), maintenance_capex_low_evidence_id, sbc_metric_evidence_id, market_cap_evidence_id],
            "Maintenance_Real_FCF_Yield_High:model-input",
        )
        coverage_gate = calculate_interest_coverage_gate(
            ebit_ttm, interest_ttm, total_debt, cash
        )
        if bool(coverage_gate["missing_critical"]):
            return ModeCResult(
                Ticker=ticker,
                Status="Abstain: 有淨負債但利息費用證據缺失",
                Sector=sector,
                Industry=industry,
                Model_Route=str(model_route["route"]),
                Model_Route_Reason=str(model_route["reason"]),
                Decision_State="ABSTAIN",
                Decision_Timestamp=decision_timestamp.isoformat(),
                Data_Confidence_Score=0.0,
                Data_Confidence_Reasons="Net debt service cannot be tested without interest-expense evidence",
            )
        icr = float(coverage_gate["icr"])
        coverage_mode = str(coverage_gate["mode"])
        interest_cash_proxy_used = bool(
            coverage_mode == "reported_interest" and interest_cash_proxy_available
        )
        if coverage_mode == "reported_interest":
            if interest_cash_proxy_used:
                coverage_mode = "cash_interest_paid_proxy"
            icr_formula = (
                "TTM EBIT / TTM cash interest paid proxy"
                if interest_cash_proxy_used
                else "TTM EBIT / TTM Interest Expense"
            )
            icr_source_ids = [
                str(ebit_evidence.get("evidence_id") or ""),
                str(interest_evidence.get("evidence_id") or ""),
            ]
        elif coverage_mode == "net_cash":
            icr_formula = "Cash fully covers total debt; ICR gate is not binding"
            icr_source_ids = [*debt_evidence_ids, *cash_evidence_ids]
        else:
            icr_formula = "Total debt is immaterial; ICR gate is not binding"
            icr_source_ids = debt_evidence_ids
        icr_evidence_id = sec._record_derived_from_ids(
            "ICR",
            icr,
            "x",
            icr_formula,
            icr_source_ids,
            "ICR:model-input",
        )

        stress_15 = calculate_financial_stress(
            ebit_ttm, ebitda, interest_ttm, total_debt, cash,
            real_fcf, effective_tax_rate, 0.15,
        )
        stress_30 = calculate_financial_stress(
            ebit_ttm, ebitda, interest_ttm, total_debt, cash,
            real_fcf, effective_tax_rate, 0.30,
        )
        stress_icr_net_cash = total_debt <= 0.01 or cash >= total_debt
        stress_icr_30_evidence_id = sec._record_derived_from_ids(
            "Stress_ICR_30",
            stress_30["icr"],
            "x",
            (
                "Cash fully covers debt after the EBITDA shock; ICR gate is not binding"
                if stress_icr_net_cash
                else "(TTM EBIT - 30% of TTM EBITDA) / TTM Interest Expense"
            ),
            (
                [*debt_evidence_ids, *cash_evidence_ids]
                if stress_icr_net_cash
                else [
                    str(ebit_evidence.get("evidence_id") or ""),
                    ebitda_evidence_id,
                    str(interest_evidence.get("evidence_id") or ""),
                ]
            ),
            "Stress_ICR_30:model-input",
        )
        stress_leverage_30_evidence_id = sec._record_derived_from_ids(
            "NetDebt_to_Stress_EBITDA_30",
            stress_30["net_debt_to_ebitda"],
            "x",
            "max(Debt - Cash, 0) / (TTM EBITDA * 70%)",
            [ebitda_evidence_id, *debt_evidence_ids, *cash_evidence_ids],
            "NetDebt_to_Stress_EBITDA_30:model-input",
        )
        stress_fcf_30_evidence_id = sec._record_derived_from_ids(
            "Stress_Real_FCF_30",
            stress_30["real_fcf_b"],
            "USD_B",
            "Real FCF - 30% EBITDA shock after tax",
            [
                real_fcf_evidence_id,
                ebitda_evidence_id,
                effective_tax_rate_evidence_id,
            ],
            "Stress_Real_FCF_30:model-input",
        )
        stress_survival_30_evidence_id = sec._record_derived_from_ids(
            "Stress_Survival_30",
            stress_30["survives"],
            "boolean",
            "stressed EBITDA > 0, debt service remains viable, and stressed FCF is nonnegative or covered by net cash",
            [
                stress_icr_30_evidence_id,
                stress_leverage_30_evidence_id,
                stress_fcf_30_evidence_id,
            ],
            "Stress_Survival_30:model-input",
        )

        real_buyback = buyback_ttm - issuance_ttm
        share_change_pct = safe_div(shares_sec_now, shares_1y_ago) * 100 - 100 if shares_sec_now > 0 and shares_1y_ago > 0 else np.nan
        share_change_3y_pct = safe_div(shares_sec_now, shares_3y_ago) * 100 - 100 if shares_sec_now > 0 and shares_3y_ago > 0 else np.nan
        share_basis_discontinuity = bool(
            (math.isfinite(share_change_pct) and abs(share_change_pct) > 50.0)
            or (math.isfinite(share_change_3y_pct) and abs(share_change_3y_pct) > 50.0)
        )
        dilution_illusion = bool(
            not share_basis_discontinuity
            and (
                (math.isfinite(share_change_pct) and share_change_pct > 0.5)
                or (real_buyback > 0 and math.isfinite(share_change_pct) and share_change_pct >= 0)
            )
        )
        persistent_dilution = bool(
            not share_basis_discontinuity
            and math.isfinite(share_change_3y_pct)
            and share_change_3y_pct > 3.0
        )
        net_buyback_yield = safe_div(real_buyback, mcap) * 100 if mcap > 0 else np.nan
        buyback_offset_effective = bool(
            real_buyback > 0
            and math.isfinite(share_change_pct)
            and share_change_pct < 0
            and not share_basis_discontinuity
        )
        sbc_attribution = {
            "economic_cost_b": round(sbc_ttm, 3),
            "ownership_dilution_1y_pct": round(share_change_pct, 2) if math.isfinite(share_change_pct) else None,
            "ownership_dilution_3y_pct": round(share_change_3y_pct, 2) if math.isfinite(share_change_3y_pct) else None,
            "net_buyback_b": round(real_buyback, 3),
            "net_buyback_yield_pct": round(net_buyback_yield, 2) if math.isfinite(net_buyback_yield) else None,
            "buyback_offset_effective": buyback_offset_effective,
            "persistent_dilution_gate": persistent_dilution,
            "share_basis_discontinuity": share_basis_discontinuity,
            "per_share_fcf_cagr_3y_pct": (
                round(float(per_share_growth["fcf_cagr_pct"]), 2)
                if math.isfinite(float(per_share_growth["fcf_cagr_pct"]))
                else None
            ),
            "per_share_eps_cagr_3y_pct": (
                round(float(per_share_growth["eps_cagr_pct"]), 2)
                if math.isfinite(float(per_share_growth["eps_cagr_pct"]))
                else None
            ),
        }
        capital_allocation_score = calculate_capital_allocation_score(
            real_buyback,
            issuance_ttm,
            np.nan if share_basis_discontinuity else share_change_pct,
            np.nan if share_basis_discontinuity else share_change_3y_pct,
            mcap,
        )
        real_buyback_evidence_id = sec._record_derived_from_ids(
            "Real_Buyback",
            real_buyback,
            "USD_B",
            "TTM cash repurchases - TTM stock issuance proceeds",
            [
                str(buyback_evidence.get("evidence_id") or ""),
                str(issuance_evidence.get("evidence_id") or ""),
            ],
            "Real_Buyback:model-input",
        )
        sec._record_derived_from_ids(
            "Net_Buyback_Yield",
            net_buyback_yield,
            "percent",
            "(TTM cash repurchases - TTM stock issuance proceeds) / Market Capitalization",
            [real_buyback_evidence_id, market_cap_evidence_id],
            "Net_Buyback_Yield:model-input",
        )
        share_history_evidence_ids = GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(
            ticker, "SharesOutstanding"
        )
        share_change_1y_evidence_id = sec._record_derived_from_ids(
            "Share_Count_Change_1Y",
            share_change_pct,
            "percent",
            "split-adjusted current reported shares / split-adjusted shares one year ago - 1",
            [*share_history_evidence_ids, *sec.share_split_evidence_ids],
            "Share_Count_Change_1Y:model-input",
        )
        share_change_3y_evidence_id = sec._record_derived_from_ids(
            "Share_Count_Change_3Y",
            share_change_3y_pct,
            "percent",
            "split-adjusted current reported shares / split-adjusted shares three years ago - 1",
            [*share_history_evidence_ids, *sec.share_split_evidence_ids],
            "Share_Count_Change_3Y:model-input",
        )
        per_share_source_ids = [
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "OCF"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "CapEx"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "SBC"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DnA"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Revenue"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "NetIncome"),
            *share_history_evidence_ids,
            *sec.share_split_evidence_ids,
        ]
        per_share_fcf_evidence_id = sec._record_derived_from_ids(
            "Per_Share_FCF_CAGR_3Y",
            per_share_growth["fcf_cagr_pct"],
            "percent",
            "three-year CAGR of split-adjusted annual Maintenance FCF per share",
            per_share_source_ids,
            "Per_Share_FCF_CAGR_3Y:model-input",
        )
        per_share_eps_evidence_id = sec._record_derived_from_ids(
            "Per_Share_EPS_CAGR_3Y",
            per_share_growth["eps_cagr_pct"],
            "percent",
            "three-year CAGR of split-adjusted annual net income per share",
            per_share_source_ids,
            "Per_Share_EPS_CAGR_3Y:model-input",
        )
        capital_allocation_evidence_id = sec._record_derived_from_ids(
            "Capital_Allocation_Score",
            capital_allocation_score,
            "score_0_100",
            "net buyback yield adjusted for one-year and three-year realized dilution",
            [
                real_buyback_evidence_id,
                share_change_1y_evidence_id,
                share_change_3y_evidence_id,
                market_cap_evidence_id,
            ],
            "Capital_Allocation_Score:model-input",
        )

        stability_source_ids = []
        for metric_name in ["OCF", "CapEx", "SBC", "DnA", "Revenue", "NetIncome"]:
            stability_source_ids.extend(
                GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, metric_name)
            )
        fcf_stability_evidence_ids = []
        for metric_name, metric_value, metric_unit, formula in [
            ("OCF_3Y_Cumulative", fcf_stability["ocf_3y_cumulative_b"], "USD_B", "sum of up to the latest three consecutive reported annual OCF values"),
            ("Real_FCF_Positive_Years_5Y", fcf_stability["positive_years"], "years", "count of positive annual maintenance-FCF observations with complete SBC evidence in up to five consecutive years"),
            ("Real_FCF_Margin_Std_5Y", fcf_stability["margin_std_pct"], "percent", "standard deviation of annual maintenance-FCF margins"),
            ("OCF_to_NetIncome_5Y", fcf_stability["ocf_to_net_income"], "x", "sum annual OCF / sum annual Net Income"),
            ("Real_FCF_to_NetIncome_5Y", fcf_stability["real_fcf_to_net_income"], "x", "sum annual maintenance-FCF / sum annual Net Income"),
        ]:
            fcf_stability_evidence_ids.append(
                sec._record_derived_from_ids(
                    metric_name,
                    metric_value,
                    metric_unit,
                    formula,
                    stability_source_ids,
                    f"{metric_name}:model-input",
                )
            )

        flags = []
        if sbc_external_fallback:
            flags.append("SBC 缺少 SEC point-in-time 證據：暫用市場資料 fallback")
        if debt_external_fallback:
            flags.append("總負債缺少可用 SEC 同期證據：暫用 Yahoo totalDebt 並降低資料信心")
        if not reported_tax_rate_usable:
            flags.append("有效稅率使用 21% 保守假設：原始 TTM 稅率缺失或受 tax benefit 扭曲")
        if interest_cash_proxy_used:
            flags.append("ICR 使用 cash interest paid 代理應計利息費用，資料信心降低")
        if coverage_mode == "net_cash" and (df_int.empty or interest_ttm <= 0):
            flags.append("現金完全覆蓋總負債：利息費用缺失時 ICR 閘門不具約束力")
        if not math.isfinite(share_change_pct):
            flags.append("一年股數歷史不足：無法驗證實質回購")
        if not math.isfinite(share_change_3y_pct):
            flags.append("三年股數歷史不足：無法判定持續稀釋")
        if share_basis_discontinuity:
            flags.append("股數口徑跳變：完成拆股調整後仍超過50%，不得自動判定稀釋")
        if not math.isfinite(roic):
            flags.append("權益或投入資本資料不足：ROIC/ROCE 待查")
        if conservative_real_fcf <= 0 < real_fcf:
            flags.append("Growth CapEx split: all-CapEx FCF is negative; inspect CapEx project mix before sizing")
        if bool(capex_profile.get("growth_capex_trap")):
            flags.append("Growth CapEx trap: CapEx is far above D&A while revenue is not growing")
        rev_q = sec.quarterly_series(df_rev, "Revenue")
        if len(rev_q) >= 8:
            rev_ttm_now = rev_q.tail(4).sum()
            rev_ttm_prev = rev_q.iloc[-8:-4].sum()
            if rev_ttm_prev > 0 and rev_ttm_now < rev_ttm_prev and capex_ttm < dna_ttm * 0.60:
                flags.append("躺平式自殺虛高 Yield：CapEx 遠低於 D&A 且 TTM 營收下滑")


        if icr < ICR_WARNING:
            flags.append(f"財務脆弱：ICR<{ICR_WARNING}")
        if not bool(stress_30["survives"]):
            flags.append(
                f"30% EBITDA 壓力後未通過存續門檻：ICR<{STRESS_ICR_MIN}x（淨現金公司除外）"
            )
        if dilution_illusion:
            flags.append("單年稀釋警示：回購未有效降低股數，本項扣分但不單獨排除")
        if persistent_dilution:
            flags.append("持續稀釋：近三年流通股數累計增加超過3%，排除")


        # 三點勾稽僅在各組成資料存在時執行，避免把缺值當成零。
        reconciliation_warning = False
        reported_yf_ebitda = finite_number(info.get("ebitda"))
        yf_ebitda = (
            reported_yf_ebitda / 1e9
            if reported_yf_ebitda is not None
            else np.nan
        )
        if math.isfinite(yf_ebitda) and yf_ebitda > 0 and ebitda > 0:
            diff_1 = safe_div(abs(ebitda - yf_ebitda), max(abs(ebitda), 0.001))
            diff_2 = np.nan
            if not df_tax.empty and (total_debt <= 0.01 or not df_int.empty):
                sec_ebitda_2 = net_income_ttm + interest_ttm + tax_ttm + dna_ttm
                diff_2 = safe_div(abs(ebitda - sec_ebitda_2), max(abs(ebitda), 0.001))
            if diff_1 > 0.05 or (math.isfinite(diff_2) and diff_2 > 0.05):
                reconciliation_warning = True
                flags.append("數據勾稽警報：EBITDA 來源差異率超出5%，需查非經常性項目")


        hv = historical_valuation(
            ticker, sec, df_ebit, df_dna, df_debt_total, df_debt_current, df_cash, df_net_income, df_shares,
            ev_ebitda, pe, shares_now,
            debt_component_frames={
                "DebtShortTermTotal": df_debt_short_total,
                "DebtOtherShortTerm": df_debt_other_short,
                "DebtCommercialPaper": df_debt_commercial_paper,
                "DebtFinanceLease": df_debt_finance_lease,
            },
        )
        ev_percentile_evidence_id = sec._record_derived_from_ids(
            "EV_EBITDA_10Y_Percentile",
            hv["ev_ebitda_percentile"],
            "percentile",
            "percentile rank of current EV/EBITDA against reconstructed point-in-time history",
            [ev_ebitda_evidence_id, *hv.get("ev_evidence_ids", [])],
            "EV_EBITDA_10Y_Percentile:model-input",
        )
        pe_percentile_evidence_id = sec._record_derived_from_ids(
            "PE_10Y_Percentile",
            hv["pe_percentile"],
            "percentile",
            "percentile rank of current P/E against reconstructed point-in-time history",
            [pe_evidence_id, *hv.get("pe_evidence_ids", [])],
            "PE_10Y_Percentile:model-input",
        )
        historical_coverage_evidence_id = sec._record_derived_from_ids(
            "Historical_Valuation_Coverage",
            hv["coverage"],
            "ratio",
            "valid point-in-time EV/EBITDA years / candidate annual periods",
            [
                ev_ebitda_evidence_id,
                *hv.get("ev_evidence_ids", []),
                *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "EBIT"),
                *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "DnA"),
            ],
            "Historical_Valuation_Coverage:decision-gate",
        )
        ev_floor = hv["ev_ebitda_floor"]
        pe_floor = hv["pe_floor"]
        if not math.isfinite(ev_floor) or ev_floor <= 0:
            ev_floor = max(min(ev_ebitda * 0.60, ev_ebitda), 4.0)
            flags.append("歷史 EV/EBITDA 不足：雙殺改用保守 fallback floor")
        if not math.isfinite(pe_floor) or pe_floor <= 0:
            pe_floor = np.nan


        def stress_drawdown(drop: float) -> float:
            stress_ebitda = ebitda * (1 - drop)
            stress_ev = stress_ebitda * ev_floor
            stress_mcap_ev = max(0.0, stress_ev - (total_debt - cash))
            stress_mcap = stress_mcap_ev
            if math.isfinite(pe_floor) and pe_floor > 0 and net_income_ttm > 0:
                stress_ni = net_income_ttm * (1 - drop)
                stress_mcap_pe = max(0.0, stress_ni * pe_floor)
                stress_mcap = min(stress_mcap_ev, stress_mcap_pe)
            return safe_div(stress_mcap - mcap, mcap) * 100


        dd15 = stress_drawdown(0.15)
        dd30 = stress_drawdown(0.30)


        gp_q = sec.quarterly_series(df_gp, "GrossProfit")
        gm_diag, gm_metrics = classify_three_quarter_trend(rev_q, gp_q)
        gross_margin_source_ids = [
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Revenue"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "GrossProfit"),
        ]
        gm_latest_evidence_id = sec._record_derived_from_ids(
            "GM_Latest",
            gm_metrics.get("gm_latest_pct", np.nan),
            "percent",
            "latest derived quarterly Gross Profit / Revenue",
            gross_margin_source_ids,
            "GM_Latest:model-input",
        )
        gm_change_evidence_id = sec._record_derived_from_ids(
            "GM_3Q_Change",
            gm_metrics.get("gm_3q_change_pp", np.nan),
            "percentage_points",
            "latest gross margin - gross margin two quarters earlier",
            gross_margin_source_ids,
            "GM_3Q_Change:model-input",
        )
        revenue_change_evidence_id = sec._record_derived_from_ids(
            "Revenue_3Q_Change",
            gm_metrics.get("rev_3q_change_pct", np.nan),
            "percent",
            "latest quarterly Revenue / Revenue two quarters earlier - 1",
            gross_margin_source_ids,
            "Revenue_3Q_Change:model-input",
        )


        target_multiples = [
            x.get("EV_EBITDA")
            for x in hv.get("ev_ebitda_hist", [])
            if x.get("EV_EBITDA", np.nan) > 0
        ]
        target_mult = float(np.median(target_multiples)) if target_multiples else np.nan
        if not math.isfinite(target_mult) or target_mult <= 0:
            target_mult = max(ev_floor, 6.0)
        implied_cagr = implied_ebitda_cagr(
            ev,
            ebitda,
            target_mult,
            required_return=REVERSE_DCF_REQUIRED_RETURN,
        ) * 100
        implied_cagr_evidence_id = sec._record_derived_from_ids(
            "Implied_EBITDA_CAGR_3Y",
            implied_cagr,
            "percent",
            "CAGR required for exit EV to compound at required return using historical target multiple",
            [ev_evidence_id, ebitda_evidence_id, *hv.get("ev_evidence_ids", [])],
            "Implied_EBITDA_CAGR_3Y:model-input",
        )
        implied_cagr_limit = dynamic_implied_cagr_limit(
            roic, gm_metrics.get("gm_3q_change_pp", np.nan)
        )
        implied_cagr_limit_evidence_id = sec._record_derived_from_ids(
            "Implied_CAGR_Limit",
            implied_cagr_limit,
            "percent",
            "dynamic limit from ROIC and three-quarter gross-margin change",
            [roic_evidence_id, gm_change_evidence_id],
            "Implied_CAGR_Limit:model-input",
        )


        # 【修正核心】：軋空動態數據旗標寫入
        si_float = info.get("shortPercentOfFloat")
        try:
            si_value = float(si_float)
        except (TypeError, ValueError):
            si_value = np.nan
        si_pct = si_value * 100 if math.isfinite(si_value) and si_value < 1 else si_value
        try:
            dtc = float(info.get("shortRatio") or info.get("daysToCover") or np.nan)
        except (TypeError, ValueError):
            dtc = np.nan
        short_age_days = short_interest_data_age_days(info, now=decision_timestamp)
        short_period = info.get("dateShortInterest") or info.get("shortInterestDate") or ""
        if math.isfinite(si_pct):
            sec.record_observed_input(
                "ShortInterestPctFloat",
                si_pct,
                "percent",
                "YAHOO_FINANCE",
                "shortPercentOfFloat",
                period_end=short_period,
            )
        if math.isfinite(dtc):
            sec.record_observed_input(
                "DaysToCover",
                dtc,
                "days",
                "YAHOO_FINANCE",
                "shortRatio/daysToCover",
                period_end=short_period,
            )
        short_data_fresh = math.isfinite(short_age_days) and short_age_days <= 45.0
        squeeze = bool(
            short_data_fresh
            and math.isfinite(si_pct)
            and math.isfinite(dtc)
            and si_pct > SHORT_SQUEEZE_SI
            and dtc > SHORT_SQUEEZE_DTC
        )
        if squeeze:
            flags.append(f"高軋空波動風險(SI={si_pct:.1f}%, DTC={dtc:.1f})：不因事件題材放寬基本面門檻")
        elif math.isfinite(si_pct) and math.isfinite(dtc) and not short_data_fresh:
            flags.append("Short Interest 日期缺失或超過45天：不啟用軋空旗標")


        cogs_q = sec.quarterly_series(df_cogs, "COGS")
        sec._mark_rows_used(sec._instant_facts(df_inv), "Inventory:DSI")
        dsi_inputs_stale = stale_required_fact_names(
            {"Inventory": df_inv, "COGS": df_cogs},
            decision_timestamp,
        )
        if dsi_inputs_stale:
            flags.append(
                "DSI 未計分：來源期間超過資料新鮮度上限 "
                + "/".join(dsi_inputs_stale)
            )
        dsi = (
            pd.Series(dtype=float)
            if dsi_inputs_stale
            else calc_dsi_series(df_inv, cogs_q, sec=sec)
        )
        dsi_signal = analyze_dsi_signal(dsi)
        dsi_latest = float(dsi_signal["latest"])
        dsi_2q_down = bool(dsi_signal["sequential_down"])
        dsi_source_ids = [
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "COGS"),
            *GLOBAL_EVIDENCE_LEDGER.selected_evidence_ids(ticker, "Inventory"),
        ]
        dsi_latest_evidence_id = sec._record_derived_from_ids(
            "DSI_Latest",
            dsi_latest,
            "days",
            "average Inventory / trailing-four-quarter COGS * 365",
            dsi_source_ids,
            "DSI_Latest:model-input",
        )
        dsi_qoq_evidence_id = sec._record_derived_from_ids(
            "DSI_QoQ_Change",
            dsi_signal["qoq_change_pct"],
            "percent",
            "latest DSI / prior-quarter DSI - 1",
            dsi_source_ids,
            "DSI_QoQ_Change:model-input",
        )
        dsi_yoy_evidence_id = sec._record_derived_from_ids(
            "DSI_YoY_Change",
            dsi_signal["yoy_change_pct"],
            "percent",
            "latest DSI / same-quarter-prior-year DSI - 1",
            dsi_source_ids,
            "DSI_YoY_Change:model-input",
        )
        inventory_inflection_evidence_id = sec._record_derived_from_ids(
            "Inventory_Inflection",
            dsi_signal["inflection"],
            "boolean",
            "two sequential DSI declines confirmed by favorable year-over-year DSI",
            [dsi_latest_evidence_id, dsi_qoq_evidence_id, dsi_yoy_evidence_id],
            "Inventory_Inflection:model-input",
        )


        catalysts = get_upcoming_earnings(ticker, info, now=decision_timestamp)
        if not catalysts:
            catalysts = ["未偵測到 30 天內財報；需 Agent 補查法說/供應鏈月營收/產業會議"]


        physical_check, agent_tasks = build_agent_verification_plan(
            ticker=ticker,
            info=info,
            implied_cagr_pct=implied_cagr,
            real_fcf_yield_pct=real_fcf_yield,
        )

        evidence_stats = GLOBAL_EVIDENCE_LEDGER.selected_source_stats(ticker)
        ttm_methods = {
            "OCF": ocf_method,
            "CapEx": capex_method,
            "SBC": sbc_method,
            "EBIT": ebit_method,
            "DnA": dna_method,
            "Revenue": rev_method,
            "NetIncome": net_income_method,
        }
        confidence = assess_data_confidence(
            evidence_stats,
            ttm_methods,
            share_change_1y_available=math.isfinite(share_change_pct),
            share_change_3y_available=math.isfinite(share_change_3y_pct),
            roic_available=math.isfinite(roic),
            valuation_history_available=hv["coverage_status"] == "VALID",
            reconciliation_warning=reconciliation_warning,
            sbc_sec_evidence_available=not sbc_external_fallback and bool(sbc_evidence.get("evidence_id")),
            current_shares_sec_evidence_available=shares_source == "SEC",
            share_basis_discontinuity=share_basis_discontinuity,
            debt_sec_evidence_available=debt_sec_evidence_available,
            debt_fully_cash_covered=cash >= total_debt,
            tax_rate_sec_evidence_available=reported_tax_rate_usable,
            interest_cash_proxy_used=interest_cash_proxy_used,
        )
        data_confidence_evidence_id = sec._record_derived_from_ids(
            "Data_Confidence_Score",
            confidence["score"],
            "score_0_100",
            "coverage gate from acceptance metadata, tag priority, period validity and critical metric completeness",
            GLOBAL_EVIDENCE_LEDGER.selected_source_evidence_ids(ticker),
            "Data_Confidence_Score:decision-gate",
        )

        # 先排除財務結構明顯不適合長期持有的公司；其餘交給多因子框架排序。
        status = "Pass"
        if gm_diag.startswith("資料不足"):
            status = "Fail: 季度毛利資料不足"
        elif math.isfinite(icr) and icr < 1.0:
            status = "Fail: ICR < 1.0，財務韌性不足"
        elif real_fcf <= 0:
            status = "Fail: Real FCF 非正值"
        elif bool(capex_profile.get("growth_capex_trap")):
            status = "Fail: Growth CapEx far above D&A without revenue growth"
        elif fcf_stability["ocf_3y_years"] >= 3 and fcf_stability["ocf_3y_cumulative_b"] <= 0:
            status = "Fail: 近三年累計 OCF 非正值"
        elif (
            minimum_positive_fcf_years(fcf_stability["years_available"]) > 0
            and fcf_stability["positive_years"]
            < minimum_positive_fcf_years(fcf_stability["years_available"])
        ):
            status = "Fail: 可得連續年度 Real FCF 正值比例不足 60%"
        elif persistent_dilution:
            status = "Fail: 近三年股數持續明顯稀釋"
        elif "雙重惡化" in gm_diag:
            status = "Fail: 營收與毛利同步惡化"
        if capex_profile.get("confidence") == "LOW":
            status = "Abstain: Maintenance CapEx estimate confidence is LOW"
        elif status == "Pass" and bool(confidence["abstain"]):
            status = f"Abstain: Data confidence {float(confidence['score']):.0f}<{MIN_DATA_CONFIDENCE:.0f}"
        decision_state = "PASS" if status == "Pass" else "ABSTAIN" if status.startswith("Abstain") else "FAIL"


        result = ModeCResult(
            Ticker=ticker,
            Status=status,
            Price=round(price, 2),
            Sector=str(info.get("sector") or ""),
            Industry=str(info.get("industry") or ""),
            Model_Route=str(model_route["route"]),
            Model_Supported=bool(model_route["supported"]),
            Model_Route_Reason=str(model_route["reason"]),
            Industry_Model_Key=model_key,
            Decision_State=decision_state,
            Decision_Timestamp=decision_timestamp.isoformat(),
            Data_Confidence_Score=float(confidence["score"]),
            Data_Confidence_Reasons="; ".join(str(reason) for reason in confidence["reasons"]),
            Evidence_Source_Count=float(evidence_stats["selected_source_count"]),
            Evidence_AcceptedAt_Ratio=round(float(evidence_stats["accepted_at_ratio"]), 3),
            MarketCap_B=round(mcap, 3),
            EV_B=round(ev, 3),
            Total_Debt_B=round(total_debt, 3),
            Cash_B=round(cash, 3),
            Net_Debt_B=round(total_debt - cash, 3),
            Debt_Source_Method=debt_method,
            Maintenance_Real_FCF_B=round(real_fcf, 3),
            Conservative_Real_FCF_B=round(conservative_real_fcf, 3),
            Real_FCF_Yield_pct=round(real_fcf_yield, 2),
            Conservative_Real_FCF_Yield_pct=round(conservative_real_fcf_yield, 2),
            Maintenance_Real_FCF_to_MarketCap_Yield_pct=round(real_fcf_yield, 2),
            Conservative_Real_FCF_to_MarketCap_Yield_pct=round(conservative_real_fcf_yield, 2),
            Maintenance_Real_FCF_to_EV_Yield_pct=round(real_fcf_to_ev_yield, 2),
            Conservative_Real_FCF_to_EV_Yield_pct=round(conservative_real_fcf_to_ev_yield, 2),
            Maintenance_Real_FCF_Yield_Low_pct=round(real_fcf_yield_low, 2),
            Maintenance_Real_FCF_Yield_High_pct=round(real_fcf_yield_high, 2),
            TTM_OCF_B=round(ocf_ttm, 3),
            Dynamic_CapEx_B=round(dynamic_capex, 3),
            Maintenance_CapEx_B=round(maintenance_capex, 3),
            Maintenance_CapEx_Low_B=round(maintenance_capex_low, 3),
            Maintenance_CapEx_High_B=round(maintenance_capex_high, 3),
            Maintenance_CapEx_Confidence=str(capex_profile["confidence"]),
            Growth_CapEx_B=round(growth_capex, 3),
            CapEx_to_DnA_x=round(float(capex_profile["capex_to_dna"]), 2) if math.isfinite(float(capex_profile["capex_to_dna"])) else np.nan,
            CapEx_Reinvestment_Method=str(capex_profile["method"]),
            TTM_SBC_B=round(sbc_ttm, 3),
            SBC_Economic_Cost_B=round(sbc_ttm, 3),
            Net_Buyback_Yield_pct=round(net_buyback_yield, 2) if math.isfinite(net_buyback_yield) else np.nan,
            Buyback_Offset_Effective=buyback_offset_effective,
            SBC_Attribution_JSON=json.dumps(sbc_attribution, ensure_ascii=False, sort_keys=True),
            Per_Share_FCF_CAGR_3Y_pct=(
                round(float(per_share_growth["fcf_cagr_pct"]), 2)
                if math.isfinite(float(per_share_growth["fcf_cagr_pct"]))
                else np.nan
            ),
            Per_Share_EPS_CAGR_3Y_pct=(
                round(float(per_share_growth["eps_cagr_pct"]), 2)
                if math.isfinite(float(per_share_growth["eps_cagr_pct"]))
                else np.nan
            ),
            ICR=round(icr, 2),
            ICR_Method=coverage_mode,
            Real_Buyback_B=round(real_buyback, 3),
            Share_Count_Change_pct=round(share_change_pct, 2) if math.isfinite(share_change_pct) else np.nan,
            Share_Count_Change_3Y_pct=round(share_change_3y_pct, 2) if math.isfinite(share_change_3y_pct) else np.nan,
            Share_Split_Factor_1Y=float(sec.share_split_factors.get("1y", 1.0)),
            Share_Split_Factor_3Y=float(sec.share_split_factors.get("3y", 1.0)),
            Share_Basis_Discontinuity=share_basis_discontinuity,
            Dilution_Illusion=dilution_illusion,
            Persistent_Dilution=persistent_dilution,
            ROIC_pct=round(roic, 2) if math.isfinite(roic) else np.nan,
            ROCE_pct=round(roce, 2) if math.isfinite(roce) else np.nan,
            OCF_3Y_Cumulative_B=round(fcf_stability["ocf_3y_cumulative_b"], 3) if math.isfinite(fcf_stability["ocf_3y_cumulative_b"]) else np.nan,
            OCF_3Y_Years=fcf_stability["ocf_3y_years"],
            Real_FCF_Positive_Years_5Y=fcf_stability["positive_years"],
            Real_FCF_Years_Available=fcf_stability["years_available"],
            Real_FCF_Margin_Std_5Y_pct=round(fcf_stability["margin_std_pct"], 2) if math.isfinite(fcf_stability["margin_std_pct"]) else np.nan,
            OCF_to_NetIncome_5Y=round(fcf_stability["ocf_to_net_income"], 2) if math.isfinite(fcf_stability["ocf_to_net_income"]) else np.nan,
            Real_FCF_to_NetIncome_5Y=round(fcf_stability["real_fcf_to_net_income"], 2) if math.isfinite(fcf_stability["real_fcf_to_net_income"]) else np.nan,
            Capital_Allocation_Score=capital_allocation_score,
            EBITDA_B=round(ebitda, 3),
            EV_EBITDA_x=round(ev_ebitda, 2),
            PE_x=round(pe, 2) if math.isfinite(pe) else np.nan,
            EV_EBITDA_10Y_Percentile=round(hv["ev_ebitda_percentile"], 1) if math.isfinite(hv["ev_ebitda_percentile"]) else np.nan,
            PE_10Y_Percentile=round(hv["pe_percentile"], 1) if math.isfinite(hv["pe_percentile"]) else np.nan,
            Historical_Valuation_Valid_Years=float(hv["valid_years"]),
            Historical_Valuation_Total_Years=float(hv["total_years"]),
            Historical_Valuation_Coverage=round(float(hv["coverage"]), 3),
            Historical_Valuation_Status=str(hv["coverage_status"]),
            Industry_Stress_Extension_Status="IMPLEMENTED",
            Industry_Stress_Extension_Reason="General corporate EBITDA -15%/-30% stress with debt-service and cash-flow survival gates",
            EBITDA_Drawdown_15_pct=round(dd15, 1),
            EBITDA_Drawdown_30_pct=round(dd30, 1),
            Stress_ICR_15x=round(float(stress_15["icr"]), 2),
            Stress_ICR_30x=round(float(stress_30["icr"]), 2),
            NetDebt_to_Stress_EBITDA_30x=round(float(stress_30["net_debt_to_ebitda"]), 2) if math.isfinite(float(stress_30["net_debt_to_ebitda"])) else np.nan,
            Stress_Real_FCF_30_B=round(float(stress_30["real_fcf_b"]), 3),
            Stress_Survival_30=bool(stress_30["survives"]),
            GM_Diagnosis=gm_diag,
            Rev_3Q_Change_pct=round(gm_metrics.get("rev_3q_change_pct", np.nan), 2),
            GM_Latest_pct=round(gm_metrics.get("gm_latest_pct", np.nan), 2),
            GM_3Q_Change_pp=round(gm_metrics.get("gm_3q_change_pp", np.nan), 2),
            Implied_EBITDA_CAGR_3Y_pct=round(implied_cagr, 2) if math.isfinite(implied_cagr) else np.nan,
            Implied_CAGR_Limit_pct=round(implied_cagr_limit, 2),
            Reverse_DCF_Required_Return_pct=REVERSE_DCF_REQUIRED_RETURN * 100,
            Momentum_12M_pct=round(pm["momentum_12m"], 2) if pm.get("momentum_12m") is not None and math.isfinite(pm["momentum_12m"]) else np.nan,
            Physical_Check=physical_check,
            ShortInterest_pctFloat=round(si_pct, 2) if math.isfinite(si_pct) else np.nan,
            DaysToCover=round(dtc, 2) if math.isfinite(dtc) else np.nan,
            Short_Data_Age_Days=round(short_age_days, 1) if math.isfinite(short_age_days) else np.nan,
            Squeeze_Risk=squeeze,
            DSI_Latest=round(dsi_latest, 1) if math.isfinite(dsi_latest) else np.nan,
            DSI_QoQ_Change_pct=round(float(dsi_signal["qoq_change_pct"]), 2) if math.isfinite(float(dsi_signal["qoq_change_pct"])) else np.nan,
            DSI_YoY_Change_pct=round(float(dsi_signal["yoy_change_pct"]), 2) if math.isfinite(float(dsi_signal["yoy_change_pct"])) else np.nan,
            DSI_2Q_Down=dsi_2q_down,
            Inventory_Inflection=bool(dsi_signal["inflection"]),
            Inventory_Signal=str(dsi_signal["signal"]),
            Operating_Inflection_Score=float(dsi_signal["score"]),
            Catalysts_30D="; ".join(catalysts),
            Data_Quality_Flags="; ".join(flags) if flags else "OK",
            Verdict="",
            Trade_Tool="",
            Agent_Tasks=agent_tasks,
        )
        result = apply_long_term_framework(result)
        if result.Decision_State != "ABSTAIN":
            final_score_source_ids = [
                data_confidence_evidence_id,
                real_fcf_yield_evidence_id,
                ev_percentile_evidence_id,
                pe_percentile_evidence_id,
                historical_coverage_evidence_id,
                icr_evidence_id,
                roic_evidence_id,
                capital_allocation_evidence_id,
                maintenance_capex_evidence_id,
                growth_capex_evidence_id,
                implied_cagr_evidence_id,
                implied_cagr_limit_evidence_id,
                gm_latest_evidence_id,
                gm_change_evidence_id,
                revenue_change_evidence_id,
                stress_icr_30_evidence_id,
                stress_leverage_30_evidence_id,
                stress_fcf_30_evidence_id,
                stress_survival_30_evidence_id,
                inventory_inflection_evidence_id,
                share_change_1y_evidence_id,
                share_change_3y_evidence_id,
                per_share_fcf_evidence_id,
                per_share_eps_evidence_id,
                momentum_evidence_id,
                *fcf_stability_evidence_ids,
            ]
            component_score_ids = []
            for metric_name, metric_value, formula in [
                ("Value_Score", result.Value_Score, "historical valuation percentiles and Real FCF yield"),
                ("Quality_Score", result.Quality_Score, "ICR, FCF quality, margin trend, ROIC and cash-flow stability"),
                ("Expectations_Score", result.Expectations_Score, "market-implied EBITDA CAGR versus dynamic tolerance"),
                ("Operating_Inflection_Score", result.Operating_Inflection_Score, "seasonally confirmed DSI inflection state"),
                ("Risk_Penalty", result.Risk_Penalty, "stress loss, dilution, valuation and operating-risk penalties"),
            ]:
                component_score_ids.append(
                    sec._record_derived_from_ids(
                        metric_name,
                        metric_value,
                        "score_0_100",
                        formula,
                        final_score_source_ids,
                        f"{metric_name}:final-output",
                    )
                )
            long_term_score_evidence_id = sec._record_derived_from_ids(
                "Long_Term_Score",
                result.Long_Term_Score,
                "score_0_100",
                "35% Quality + 30% Value + 20% Expectations + 5% Momentum + 5% Operating Inflection + 5% Capital Allocation - Risk Penalty",
                [*component_score_ids, capital_allocation_evidence_id],
                "Long_Term_Score:final-output",
            )
            sec._record_derived_from_ids(
                "Long_Term_Eligible",
                result.Long_Term_Eligible,
                "boolean",
                "hard eligibility gates plus minimum long-term score",
                [long_term_score_evidence_id, data_confidence_evidence_id, stress_survival_30_evidence_id],
                "Long_Term_Eligible:final-output",
            )
        return result
    except Exception as e:
        logger.error(f"[{ticker}] pipeline error: {str(e)[:120]}")
        return ModeCResult(Ticker=ticker, Status=f"Error: {str(e)[:80]}")


def run_mode_c_pipeline(
    ticker: str,
    cik: str,
    email: str,
    decision_timestamp: Optional[pd.Timestamp] = None,
    security_metadata: Optional[Dict[str, str]] = None,
) -> ModeCResult:
    result = _run_mode_c_pipeline_core(
        ticker,
        cik,
        email,
        decision_timestamp,
    )
    metadata = security_metadata or {}
    result.Input_Security_Class = str(
        metadata.get("SecurityClass") or "UNSPECIFIED_MANUAL_INPUT"
    )
    result.Security_Class_Confidence = str(
        metadata.get("SecurityClassConfidence") or "UNSPECIFIED"
    )
    result.Security_Class_Evidence_Source = str(
        metadata.get("SecurityClassEvidenceSource") or "Manual input without hunter evidence"
    )
    fx_rate = finite_number(metadata.get("PointInTimeFXRate"))
    adr_ratio = finite_number(metadata.get("ADRRatio"))
    result.Point_in_Time_FX_Rate = fx_rate if fx_rate is not None else np.nan
    result.ADR_Ratio = adr_ratio if adr_ratio is not None else np.nan
    initial_model_key = str(metadata.get("IndustryModelKey") or "")
    has_verified_route = metadata.get("_HasVerifiedRoute") == "True"
    if has_verified_route and not initial_model_key:
        initial_model_key = "GENERAL_CORPORATE"
    result.Initial_Industry_Model_Key = initial_model_key or "UNSPECIFIED"
    result.Model_Route_Refined = bool(
        initial_model_key
        and result.Model_Route_Reason
        and result.Industry_Model_Key != initial_model_key
    )
    if not result.Sector:
        result.Sector = str(metadata.get("Sector") or "")
    if not result.Industry:
        result.Industry = str(metadata.get("Industry") or "")
    if not result.Model_Route_Reason and initial_model_key:
        result.Model_Route = str(metadata.get("ModelRouteHint") or result.Model_Route)
        result.Model_Route_Reason = str(
            metadata.get("RouteReason")
            or "Monthly verified route retained before the market-data gate"
        )
        result.Industry_Model_Key = initial_model_key
        result.Model_Supported = True
        if initial_model_key != "GENERAL_CORPORATE":
            result.Scoring_Framework = (
                f"INDUSTRY_SPECIALIZED_{initial_model_key}_V1"
            )
    if result.Input_Security_Class == "COMMON_ADS_INFERRED" and (
        not math.isfinite(result.Point_in_Time_FX_Rate)
        or not math.isfinite(result.ADR_Ratio)
        or result.Point_in_Time_FX_Rate <= 0
        or result.ADR_Ratio <= 0
    ):
        result.Status = "Abstain: ADS lacks point-in-time FX rate or ADR ratio"
        result.Decision_State = "ABSTAIN"
        result.Long_Term_Score = np.nan
        result.Long_Term_Eligible = False
        result.Research_Action = "ABSTAIN_PENDING_ADR_RECONCILIATION"
        result.Data_Confidence_Reasons = "; ".join(
            filter(
                None,
                [
                    result.Data_Confidence_Reasons,
                    "Foreign/ADS valuation cannot be reconciled without point-in-time FX and ADR ratio",
                ],
            )
        )
    return result


# ===============================================================================
# 報告產生
# ==============================================================================
def render_stock_report(r: ModeCResult) -> str:
    if r.Scoring_Framework.startswith("INDUSTRY_SPECIALIZED_"):
        try:
            metrics = json.loads(r.Industry_Model_Metrics_JSON or "{}")
        except json.JSONDecodeError:
            metrics = {}
        try:
            components = json.loads(r.Industry_Model_Components_JSON or "{}")
        except json.JSONDecodeError:
            components = {}

        def display(value: Any) -> str:
            if value is None:
                return "N/A"
            if isinstance(value, float):
                return f"{value:.3f}"
            return str(value)

        lines = [
            f"## {r.Ticker} — {r.Verdict}",
            "",
            f"- 產業：{r.Sector or 'N/A'} / {r.Industry or 'N/A'}",
            f"- 專用模型：{r.Industry_Model_Key}；路由：{r.Model_Route}；決策：{r.Decision_State}",
            f"- 專用模型分數：{display(r.Industry_Model_Score)} / 100；計分覆蓋：{display(r.Industry_Model_Coverage)}%",
            f"- 資料信心：{r.Data_Confidence_Score:.1f}/100；EDGAR acceptance 比例：{display(r.Evidence_AcceptedAt_Ratio)}",
            f"- 負債 / 現金 / 淨負債：{display(r.Total_Debt_B)}B / {display(r.Cash_B)}B / {display(r.Net_Debt_B)}B；來源：{r.Debt_Source_Method or 'N/A'}",
            f"- 研究動作：{r.Research_Action}",
            f"- 建議起始權重：{r.Suggested_Starter_Weight_pct_Total:.1f}% 總資產",
            "",
            "### 專用構面",
            "",
            "| 構面 | 分數 |",
            "|---|---:|",
        ]
        for name, value in sorted(components.items()):
            lines.append(f"| {name} | {display(value)} |")
        lines.extend(["", "### 專用指標", "", "| 指標 | 數值 |", "|---|---:|"])
        for name, value in sorted(metrics.items()):
            if isinstance(value, list):
                value = ", ".join(display(item) for item in value)
            lines.append(f"| {name} | {display(value)} |")
        lines.extend(
            [
                "",
                "### 證據與人工覆核",
                "",
                f"- 硬性失敗：{r.Industry_Model_Hard_Failures or '無'}",
                f"- 模型警示：{r.Industry_Model_Warnings or '無'}",
                f"- 信心降級：{r.Data_Confidence_Reasons or '無'}",
                f"- 非標準揭露缺口：{r.Data_Quality_Flags or '無'}",
                "- 待辦：" + "；".join(r.Agent_Tasks),
                "",
            ]
        )
        return "\n".join(lines)

    lines = []
    lines.append(f"## {r.Ticker} — {r.Verdict}")
    lines.append("")
    lines.append(f"- 產業：{r.Sector or 'N/A'} / {r.Industry or 'N/A'}")
    lines.append(f"- 模型路由：{r.Model_Route}；決策狀態：{r.Decision_State}")
    lines.append(f"- 資料信心：{r.Data_Confidence_Score:.1f}/100；EDGAR acceptance 覆蓋率：{r.Evidence_AcceptedAt_Ratio if math.isfinite(r.Evidence_AcceptedAt_Ratio) else 'N/A'}")
    lines.append(f"- 長期綜合分數：{r.Long_Term_Score:.2f} / 100")
    lines.append(f"- 研究動作：{r.Research_Action}")
    lines.append(f"- 建議起始權重：{r.Suggested_Starter_Weight_pct_Total:.1f}% 總資產；單一公司上限 {MAX_POSITION_WEIGHT_PCT_TOTAL:.1f}%")
    lines.append("")
    lines.append("### 六構面評分")
    lines.append("")
    lines.append("| 構面 | 分數/數值 |")
    lines.append("|---|---:|")
    lines.append(f"| 價值分數 | {r.Value_Score:.2f} |")
    lines.append(f"| 品質分數 | {r.Quality_Score:.2f} |")
    lines.append(f"| 市場預期分數 | {r.Expectations_Score:.2f} |")
    lines.append(f"| 營運拐點分數 | {r.Operating_Inflection_Score:.2f} |")
    lines.append(f"| 資本配置分數 | {r.Capital_Allocation_Score:.2f} |")
    lines.append(f"| 風險扣分 | -{r.Risk_Penalty:.2f} |")
    lines.append(f"| Real FCF Yield (maintenance CapEx) | {r.Real_FCF_Yield_pct:.2f}% |")
    lines.append(f"| Conservative FCF Yield (all CapEx) | {r.Conservative_Real_FCF_Yield_pct if math.isfinite(r.Conservative_Real_FCF_Yield_pct) else 'N/A'}% |")
    lines.append(f"| Maintenance / Growth CapEx | {r.Maintenance_CapEx_B if math.isfinite(r.Maintenance_CapEx_B) else 'N/A'}B / {r.Growth_CapEx_B if math.isfinite(r.Growth_CapEx_B) else 'N/A'}B |")
    lines.append(f"| EV/EBITDA 10Y 分位 | {r.EV_EBITDA_10Y_Percentile if math.isfinite(r.EV_EBITDA_10Y_Percentile) else 'N/A'} |")
    lines.append(f"| 總負債 / 現金 / 淨負債 | {r.Total_Debt_B:.3f}B / {r.Cash_B:.3f}B / {r.Net_Debt_B:.3f}B |")
    lines.append(f"| ICR / 30%壓力 ICR | {r.ICR:.2f}x / {r.Stress_ICR_30x:.2f}x（{r.ICR_Method or 'N/A'}） |")
    lines.append(f"| 30%壓力淨負債 / EBITDA | {r.NetDebt_to_Stress_EBITDA_30x if math.isfinite(r.NetDebt_to_Stress_EBITDA_30x) else 'N/A'}x |")
    lines.append(f"| ROIC / ROCE | {r.ROIC_pct if math.isfinite(r.ROIC_pct) else 'N/A'}% / {r.ROCE_pct if math.isfinite(r.ROCE_pct) else 'N/A'}% |")
    lines.append(f"| 5Y Real FCF 正值年數 | {r.Real_FCF_Positive_Years_5Y:.0f} / {r.Real_FCF_Years_Available:.0f} |")
    lines.append(f"| 5Y OCF / Net Income | {r.OCF_to_NetIncome_5Y if math.isfinite(r.OCF_to_NetIncome_5Y) else 'N/A'}x |")
    lines.append(f"| 市場隱含 3Y EBITDA CAGR / 動態上限 / 餘裕 | {r.Implied_EBITDA_CAGR_3Y_pct if math.isfinite(r.Implied_EBITDA_CAGR_3Y_pct) else 'N/A'}% / {r.Implied_CAGR_Limit_pct if math.isfinite(r.Implied_CAGR_Limit_pct) else 'N/A'}% / {r.Implied_CAGR_Headroom_pct if math.isfinite(r.Implied_CAGR_Headroom_pct) else 'N/A'}pp |")
    lines.append(f"| 反向估值必要報酬 | {r.Reverse_DCF_Required_Return_pct:.1f}% |")
    lines.append(f"| 12M 動能（僅輔助） | {r.Momentum_12M_pct if math.isfinite(r.Momentum_12M_pct) else 'N/A'}% |")
    lines.append("")
    lines.append("### 下檔與論點驗證")
    lines.append("")
    lines.append(f"- EBITDA -30% 壓力情境：{r.EBITDA_Drawdown_30_pct:.1f}%")
    lines.append(f"- 財務存續壓力：ICR={r.Stress_ICR_30x:.2f}x；壓力 Real FCF={r.Stress_Real_FCF_30_B:.3f}B；通過={r.Stress_Survival_30}")
    lines.append(f"- 存貨訊號：{r.Inventory_Signal}；DSI QoQ={r.DSI_QoQ_Change_pct if math.isfinite(r.DSI_QoQ_Change_pct) else 'N/A'}%；YoY={r.DSI_YoY_Change_pct if math.isfinite(r.DSI_YoY_Change_pct) else 'N/A'}%")
    lines.append(f"- 毛利診斷：{r.GM_Diagnosis}")
    lines.append(f"- 股數變化：1Y={r.Share_Count_Change_pct if math.isfinite(r.Share_Count_Change_pct) else 'N/A'}%；3Y={r.Share_Count_Change_3Y_pct if math.isfinite(r.Share_Count_Change_3Y_pct) else 'N/A'}%；單年警示={r.Dilution_Illusion}；持續稀釋={r.Persistent_Dilution}")
    lines.append(f"- 加碼紀律：至少等一次財報，確認 thesis、Real FCF、股數與估值未惡化後才可加碼")
    lines.append(f"- 強制檢討：分數跌破60、Real FCF轉負、ICR<3、30%壓力未通過、連兩季營收與毛利惡化、明顯稀釋或 thesis 被證偽")
    lines.append(f"- 軋空風險：{r.Squeeze_Risk}；資料年齡={r.Short_Data_Age_Days if math.isfinite(r.Short_Data_Age_Days) else 'N/A'}天（只作風險旗標）")
    lines.append(f"- 數據品質：{r.Data_Quality_Flags}")
    lines.append(f"- 信心降級原因：{r.Data_Confidence_Reasons or '無'}")
    lines.append(f"- 產業物理限制：{r.Physical_Check}")
    lines.append(f"- 近期事件：{r.Catalysts_30D}")
    lines.append("")
    return "\n".join(lines)



def build_agent_payload(
    results: List[ModeCResult],
    metric_metadata_by_ticker: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict:
    metric_metadata_by_ticker = metric_metadata_by_ticker or {}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mission": "QQQ 40% + VOO 30% + 最多 30% 主動選股的長期價值研究。只做多、不使用槓桿、期權或放空；模型只產生研究候選，不是自動買入訊號。",
        "portfolio_limits": {
            "qqq_target_pct_total": 40.0,
            "voo_target_pct_total": 30.0,
            "active_sleeve_range_pct_total": [0.0, ACTIVE_SLEEVE_LIMIT_PCT],
            "starter_weight_range_pct_total": [STARTER_WEIGHT_MIN_PCT_TOTAL, STARTER_WEIGHT_PCT_TOTAL],
            "max_position_weight_pct_total": MAX_POSITION_WEIGHT_PCT_TOTAL,
            "max_sector_weight_pct_total": MAX_SECTOR_WEIGHT_PCT_TOTAL,
            "max_names": TARGET_SHORTLIST_SIZE,
            "max_names_per_sector": MAX_PER_SECTOR,
        },
        "research_shortlist": [r.Ticker for r in results if r.Long_Term_Eligible],
        "tasks": [],
    }
    for r in results:
        if not r.Long_Term_Eligible:
            continue
        payload["tasks"].append(
            {
                "ticker": r.Ticker,
                "sector": r.Sector,
                "industry": r.Industry,
                "scoring_framework": r.Scoring_Framework,
                "model_route": r.Model_Route,
                "industry_model": {
                    "key": r.Industry_Model_Key,
                    "decision": r.Industry_Model_Decision,
                    "score": r.Industry_Model_Score,
                    "coverage_pct": r.Industry_Model_Coverage,
                    "metrics": json.loads(r.Industry_Model_Metrics_JSON or "{}"),
                    "components": json.loads(r.Industry_Model_Components_JSON or "{}"),
                    "warnings": r.Industry_Model_Warnings,
                    "hard_failures": r.Industry_Model_Hard_Failures,
                },
                "decision_state": r.Decision_State,
                "data_confidence_score": r.Data_Confidence_Score,
                "data_confidence_reasons": r.Data_Confidence_Reasons,
                "long_term_score": r.Long_Term_Score,
                "research_action": r.Research_Action,
                "suggested_starter_weight_pct_total": r.Suggested_Starter_Weight_pct_Total,
                "buy_thresholds": {
                    "normal_stock": SMALL_POSITION_SCORE,
                    "qqq_or_voo_top_10": ETF_TOP10_MIN_BUY_SCORE,
                },
                "must_verify": r.Agent_Tasks + [
                    "1. 用三句話寫出投資論點",
                    "2. 寫出最強反方論點",
                    "3. 明確列出 thesis 失效條件",
                    "4. 建立悲觀/基準/樂觀三情境",
                    "5. 查核最新年度申報（10-K/20-F/40-F）、10-Q 與法說警訊",
                    "6. 查核一年及三年股數稀釋",
                    "7. 評估回購、增發、併購、股息與再投資等資本配置",
                    "8. 以最新資料查核是否為 QQQ/VOO 成分及前十大持股",
                    "9. 若已透過 ETF 持有，說明為何仍值得主動加碼；理由不足則降級",
                    "若 ETF 重疊高，另加 5-10 分決策門檻或降低主動部位，不得假裝已反映在 Quant 分數",
                    "加碼前至少等待一次財報，確認 thesis、該產業專用 KPI、股數與估值未惡化",
                    "檢查強制賣出/檢討條件：分數<60、專用模型硬性風險觸發、資料信心<70、資本配置失控或 thesis 被證偽",
                    "檢查估值退出條件：該產業核心估值已高於合理樂觀情境，或預期報酬低於最低門檻",
                ],
                "numbers_to_challenge": {
                    "Value_Score": r.Value_Score,
                    "Quality_Score": r.Quality_Score,
                    "Expectations_Score": r.Expectations_Score,
                    "Risk_Penalty": r.Risk_Penalty,
                    "Capital_Allocation_Score": r.Capital_Allocation_Score,
                    "ROIC_pct": r.ROIC_pct,
                    "ROCE_pct": r.ROCE_pct,
                    "Maintenance_CapEx_B": r.Maintenance_CapEx_B,
                    "Growth_CapEx_B": r.Growth_CapEx_B,
                    "Conservative_Real_FCF_Yield_pct": r.Conservative_Real_FCF_Yield_pct,
                    "Real_FCF_Positive_Years_5Y": r.Real_FCF_Positive_Years_5Y,
                    "Share_Count_Change_3Y_pct": r.Share_Count_Change_3Y_pct,
                    "Real_FCF_Yield_pct": r.Real_FCF_Yield_pct,
                    "EV_EBITDA_10Y_Percentile": r.EV_EBITDA_10Y_Percentile,
                    "Implied_EBITDA_CAGR_3Y_pct": r.Implied_EBITDA_CAGR_3Y_pct,
                    "Implied_CAGR_Limit_pct": r.Implied_CAGR_Limit_pct,
                    "Implied_CAGR_Headroom_pct": r.Implied_CAGR_Headroom_pct,
                    "Reverse_DCF_Required_Return_pct": r.Reverse_DCF_Required_Return_pct,
                    "EBITDA_Drawdown_30_pct": r.EBITDA_Drawdown_30_pct,
                    "Stress_ICR_30x": r.Stress_ICR_30x,
                    "Total_Debt_B": r.Total_Debt_B,
                    "Cash_B": r.Cash_B,
                    "Net_Debt_B": r.Net_Debt_B,
                    "Debt_Source_Method": r.Debt_Source_Method,
                    "ICR_Method": r.ICR_Method,
                    "NetDebt_to_Stress_EBITDA_30x": r.NetDebt_to_Stress_EBITDA_30x,
                    "Stress_Real_FCF_30_B": r.Stress_Real_FCF_30_B,
                    "Inventory_Signal": r.Inventory_Signal,
                    "DSI_YoY_Change_pct": r.DSI_YoY_Change_pct,
                    "Industry_Model_Score": r.Industry_Model_Score,
                    "Industry_Model_Coverage": r.Industry_Model_Coverage,
                    "Industry_Model_Metrics_JSON": r.Industry_Model_Metrics_JSON,
                },
                "metric_metadata": metric_metadata_by_ticker.get(r.Ticker, {}),
            }
        )
    return payload



def send_email_report(markdown: str, csv_path: str, receiver_email: str) -> None:
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    if not sender_email or not sender_pwd:
        logger.warning("未設定 EMAIL_SENDER / EMAIL_PASSWORD，略過寄信。")
        return
    msg = EmailMessage()
    msg["Subject"] = f"[Mode C 長期價值] 研究名單 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content(markdown)
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_pwd)
        server.send_message(msg)
        logger.info("Email sent.")


# ==============================================================================
# 主程式：長期價值多因子研究漏斗（分數優先、最多 12 檔）
# ==============================================================================
def main() -> None:
    GLOBAL_EVIDENCE_LEDGER.reset()
    user_email = os.environ.get("USER_EMAIL") or "a7924177@gmail.com"
    input_file = os.environ.get("QUALIFIED_UNIVERSE", QUALIFIED_UNIVERSE)
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"找不到 {input_file}，請準備欄位 Ticker, CIK 的初選清單。")

    df = pd.read_csv(input_file)
    if "Ticker" not in df.columns or "CIK" not in df.columns:
        raise ValueError("qualified_universe.csv 必須包含 Ticker, CIK 欄位。")
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    df["CIK"] = df["CIK"].astype(str).str.replace(".0", "", regex=False).str.zfill(10)
    raw_tickers = list(dict.fromkeys(df["Ticker"].tolist()))
    security_metadata_by_ticker: Dict[str, Dict[str, str]] = {}
    has_verified_route = "IndustryModelKey" in df.columns
    for _, row in df.iterrows():
        ticker = str(row.get("Ticker") or "").upper().strip()
        if not ticker:
            continue
        metadata: Dict[str, str] = {}
        for field in (
            "SecurityClass",
            "SecurityClassConfidence",
            "SecurityClassEvidenceSource",
            "Sector",
            "Industry",
            "IndustryModelKey",
            "ModelRouteHint",
            "RouteReason",
            "PointInTimeFXRate",
            "ADRRatio",
        ):
            value = row.get(field, "")
            metadata[field] = "" if pd.isna(value) else str(value)
        metadata["_HasVerifiedRoute"] = str(has_verified_route)
        security_metadata_by_ticker[ticker] = metadata

    # 每月排程更新全市場名單；push/PR 僅重跑 Mode C 與測試。
    pre_fetch_all_market_data(raw_tickers + ["SPY", "QQQ", "^TNX"], period="10y")
    pre_fetch_all_info(raw_tickers)
    hydrate_info_cache_from_verified_universe(df)
    universe = prepare_uploaded_universe(df)
    run_decision_timestamp = pd.Timestamp.now(tz="UTC").tz_convert(None)

    logger.info(f"開始長期價值清算：驗證後 {len(universe)} 檔 / 上傳 {len(raw_tickers)} 檔")
    max_workers = int(os.environ.get("MODE_C_WORKERS", "3"))
    universe_items = list(universe.items())
    results: List[Optional[ModeCResult]] = [None] * len(universe_items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(
                run_mode_c_pipeline,
                ticker,
                cik,
                user_email,
                run_decision_timestamp,
                security_metadata_by_ticker.get(ticker),
            ): index
            for index, (ticker, cik) in enumerate(universe_items)
        }
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            results[futures[future]] = future.result()
            if completed % 25 == 0 or completed == len(futures):
                logger.info("Mode C 深篩進度 %d/%d", completed, len(futures))
    results = [result for result in results if result is not None]
    for result in results:
        if not result.Decision_Timestamp:
            result.Decision_Timestamp = run_decision_timestamp.isoformat()

    shortlist = select_diversified_shortlist(results)
    eligible_count = sum(1 for r in results if r.Long_Term_Eligible)
    sector_policy = "no hard sector count cap" if MAX_PER_SECTOR <= 0 else f"max {MAX_PER_SECTOR} names per sector"
    logger.info(
        f"長期價值篩選完成：合格 {eligible_count} 檔，分數優先研究名單 {len(shortlist)} 檔；"
        f"{sector_policy}。"
    )

    # 全量結果保留，方便檢查落選原因。
    rows = [asdict(r) for r in results]
    for row in rows:
        row["Agent_Tasks"] = " | ".join(row.get("Agent_Tasks", []))
    evidence_rows = GLOBAL_EVIDENCE_LEDGER.rows(selected_only=True)
    evidence_df = pd.DataFrame(evidence_rows, columns=EVIDENCE_COLUMNS)
    evidence_df.to_csv(OUTPUT_EVIDENCE_CSV, index=False, encoding="utf-8-sig")

    rows = annotate_rows(rows, evidence_rows)
    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    # 另存真正需要深入研究的 8–12 檔候選；不足時不拿低品質公司硬湊數。
    shortlist_order = {result.Ticker: index for index, result in enumerate(shortlist)}
    shortlist_df = out_df[out_df["Ticker"].isin(shortlist_order)].copy()
    if not shortlist_df.empty:
        shortlist_df["_shortlist_order"] = shortlist_df["Ticker"].map(shortlist_order)
        shortlist_df = shortlist_df.sort_values("_shortlist_order").drop(columns="_shortlist_order")
    shortlist_df.to_csv(OUTPUT_SHORTLIST_CSV, index=False, encoding="utf-8-sig")

    report = "# Mode C 長期價值研究名單（QQQ 40% + VOO 30% + 最多 30% 主動選股）\n\n"
    report += f"清算時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    report += (
        f"**紀律：只做多、不使用槓桿/期權/放空；主動部位上限 {ACTIVE_SLEEVE_LIMIT_PCT:.0f}%；"
        f"單一公司上限 {MAX_POSITION_WEIGHT_PCT_TOTAL:.1f}%；單一產業上限 {MAX_SECTOR_WEIGHT_PCT_TOTAL:.1f}%；"
        "模型是研究漏斗，不是自動買入訊號。**\n\n"
    )
    if not shortlist:
        report += "本次沒有公司同時通過品質、估值、預期與下檔風險門檻；保留現金或 ETF，不硬湊個股。\n"
    for r in shortlist:
        report += render_stock_report(r) + "\n---\n\n"
    Path(OUTPUT_MD).write_text(report, encoding="utf-8")

    metric_metadata_by_ticker = {
        str(row["Ticker"]): json.loads(str(row["Metric_Metadata_JSON"]))
        for row in rows
    }
    payload = build_agent_payload(shortlist, metric_metadata_by_ticker)
    Path(OUTPUT_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    save_json_cache(CACHE_FILE_SHARES, _VECTOR_CACHE)

    logger.info(f"完成。Eligible={eligible_count} / Shortlist={len(shortlist)} / Total={len(out_df)}")
    logger.info(
        f"已輸出 {OUTPUT_SHORTLIST_CSV}、{OUTPUT_MD}、{OUTPUT_JSON} 與 {OUTPUT_EVIDENCE_CSV}。"
    )

    if os.environ.get("SEND_EMAIL", "0") == "1":
        send_email_report(report, OUTPUT_SHORTLIST_CSV, user_email)



if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.critical(f"系統崩潰：{exc}")
        traceback.print_exc()
        raise
