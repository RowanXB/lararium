"""The Flex query's sections and fields: the single source for the parser and the normalizer.

Names are IBKR's XML attribute names. Populated sections (Account Information, Cash Report,
Cash Transactions, Conversion Rates, Transfers, NAV) use the spellings of a real statement.
Open Positions, Prior Period Positions, Trades and Corporate Actions had no real rows when this
was written; their spellings come from IBKR's field reference, so a wrong one surfaces as a
missing-required error or a flag on the first real row, never as silent loss.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SectionSpec:
    record_type: str
    required: frozenset[str]
    known: frozenset[str]
    # The one levelOfDetail that is read, or None for every level.
    level: str | None


def _f(names: str) -> frozenset[str]:
    return frozenset(names.split())


# Fields that identify an instrument; stored on the instrument and its identifiers.
INSTRUMENT_FIELDS = _f(
    """assetCategory symbol description conid securityID securityIDType cusip isin figi
    listingExchange subCategory multiplier issuer issuerCountryCode underlyingConid
    underlyingSymbol underlyingSecurityID underlyingListingExchange strike expiry putCall
    principalAdjustFactor commodityType deliveryType fineness weight serialNumber"""
)

# Stored with the row, but never put in a log, warning or message.
PERSONAL_FIELDS = _f(
    """name street street2 city state country postalCode streetResidentialAddress
    street2ResidentialAddress cityResidentialAddress stateResidentialAddress
    countryResidentialAddress postalCodeResidentialAddress primaryEmail dateOfBirth
    beneficiaryDesignations accountRepName accountRepPhone masterName"""
)

# Sections (container elements) whose content is derivable from what is stored; skipped silently.
DERIVABLE_SECTIONS = _f(
    """StmtFunds MTMPerformanceSummaryInBase ChangeInNAV ChangeInPositionValues
    FIFOPerformanceSummaryInBase MTDYTDPerformanceSummary"""
)

_COMMON = _f("accountId acctAlias model currency fxRateToBase")

_ACCOUNT_INFORMATION = _f(
    """accountCapabilities accountId accountRepName accountRepPhone accountType acctAlias
    beneficiaryDesignations city cityResidentialAddress country countryResidentialAddress
    currency customerType dateClosed dateFunded dateOfBirth dateOpened
    dividendReinvestmentEnabled hasPendingTasks ibEntity lastTradedDate masterName model name
    postalCode postalCodeResidentialAddress primaryEmail recurringTransactions state
    stateResidentialAddress street street2 street2ResidentialAddress streetResidentialAddress
    syepEnrollmentStatus taxLotMatchingMethod tradingPermissions"""
)

_CASH_REPORT = _f(
    """accountId accountTransfers acctAlias advisorFees billPay billableCommissions
    billableSalesTax bondInterest brokerFees brokerInterest carbonCredits cashSettlingMtm
    cfdCharges clientFees commissionCreditsRedemption commissions currency debitCardActivity
    depositWithdrawals deposits dividends donations endingCash endingSettledCash
    excessFundSweep feesReceivables fromDate fxTranslationGainLoss insuredDepositInterest
    internalTransfers ipoSubscription levelOfDetail linkingAdjustments model netTradesPurchases
    netTradesSales other otherFees otherIncome paxosTransfers paymentInLieu realizedForexVm
    realizedVm referralFee salesTax slbEndingCashCollateral slbNetCash
    slbNetSecuritiesLentActivity slbNetSettledCash slbStartingCashCollateral startingCash
    taxReceivables toDate transactionTax withdrawals withholding871m withholdingCollectedTax
    withholdingTax"""
)

_EQUITY_SUMMARY = _f(
    """accountId acctAlias bondInterestAccrualsComponent bonds brokerCashComponent
    brokerFeesAccrualsComponent brokerInterestAccrualsComponent cash cfdUnrealizedPl
    cgtWithholdingAccruals commodities crypto currency dividendAccruals
    eventContractInterestAccruals fdicInsuredAccountInterestAccrualsComponent
    fdicInsuredBankSweepAccountCashComponent forexCfdUnrealizedPl funds
    incentiveCouponAccruals insuredBankDepositRedemptionCashComponent interestAccruals
    ipoSubscription liteSurchargeAccruals marginFinancingChargeAccruals model notes options
    physDel providerCryptoCash reportDate slbCashCollateral slbDirectSecuritiesBorrowed
    slbDirectSecuritiesLent softDollars stock total"""
)

_CONVERSION_RATE = _f("fromCurrency rate reportDate toCurrency")

_CASH_TRANSACTION = _f(
    """accountId acctAlias actionID amount assetCategory availableForTradingDate
    clientReference code commodityType conid currency cusip dateTime deliveryType description
    dividendType exDate expiry figi fineness fxRateToBase isin issuer issuerCountryCode
    levelOfDetail listingExchange model multiplier principalAdjustFactor putCall reportDate
    securityID securityIDType serialNumber settleDate strike subCategory symbol tradeID
    transactionID type underlyingConid underlyingListingExchange underlyingSecurityID
    underlyingSymbol weight"""
)

_TRANSFER = _f(
    """account accountId accountName acctAlias assetCategory cashTransfer clientReference code
    commodityType company conid cost currency cusip date dateTime deliveringBroker
    deliveryType description direction expiry figi fineness fxRateToBase isin issuer
    issuerCountryCode levelOfDetail listingExchange model multiplier openDateTime pnlAmount
    pnlAmountInBase positionAmount positionAmountInBase positionInstructionID
    positionInstructionSetID principalAdjustFactor putCall quantity reportDate securityID
    securityIDType serialNumber settleDate strike subCategory symbol transactionID
    transferPrice type underlyingConid underlyingListingExchange underlyingSecurityID
    underlyingSymbol weight"""
)

_OPEN_POSITION = (
    _COMMON
    | INSTRUMENT_FIELDS
    | _f(
        """reportDate position markPrice positionValue positionValueInBase openPrice
        costBasisPrice costBasisMoney fifoPnlUnrealized percentOfNAV side levelOfDetail
        openDateTime holdingPeriodDateTime code originatingOrderID originatingTransactionID
        accruedInt vestingDate unrealizedCapitalGainsPnl unrealizedlFxPnl"""
    )
)

_PRIOR_PERIOD_POSITION = (
    _COMMON | INSTRUMENT_FIELDS | _f("date price priorMtmPnl reportDate levelOfDetail")
)

_TRADE = (
    _COMMON
    | INSTRUMENT_FIELDS
    | _f(
        """reportDate dateTime tradeDate settleDateTarget transactionType exchange quantity
        tradePrice tradeMoney proceeds taxes ibCommission ibCommissionCurrency netCash
        netCashInBase closePrice openCloseIndicator notes cost fifoPnlRealized fxPnl mtmPnl
        origTradePrice origTradeDate origTradeID origOrderID origTransactionID buySell
        clearingFirmID ibOrderID transactionID ibExecID relatedTransactionID rtn
        brokerageOrderID orderReference volatilityOrderLink exchOrderId extExecID orderTime
        openDateTime holdingPeriodDateTime whenRealized whenReopened levelOfDetail
        changeInPrice changeInQuantity orderType traderID isAPIOrder accruedInt
        initialInvestment positionActionID tradeID code"""
    )
)

_CORPORATE_ACTION = (
    _COMMON
    | INSTRUMENT_FIELDS
    | _f(
        """reportDate dateTime actionDescription amount proceeds value quantity
        fifoPnlRealized mtmPnl code type transactionID actionID levelOfDetail"""
    )
)

_EVERY = _f("accountId currency")


def _spec(record_type: str, required: str, known: frozenset[str], level: str | None) -> SectionSpec:
    return SectionSpec(record_type, _f(required), known, level)


SECTIONS: Mapping[str, SectionSpec] = {
    "AccountInformation": _spec(
        "ibkr.account_information", "accountId currency dateOpened", _ACCOUNT_INFORMATION, None
    ),
    "OpenPosition": _spec(
        "ibkr.open_position", "accountId currency reportDate position", _OPEN_POSITION, None
    ),
    "PriorPeriodPosition": _spec(
        "ibkr.prior_period_position",
        "accountId currency date price",
        _PRIOR_PERIOD_POSITION,
        None,
    ),
    "CashReportCurrency": _spec(
        "ibkr.cash_report",
        "accountId currency fromDate toDate endingCash",
        _CASH_REPORT,
        "Currency",
    ),
    "Trade": _spec(
        "ibkr.trade",
        "accountId currency transactionID reportDate dateTime quantity tradePrice proceeds"
        " ibCommission ibCommissionCurrency",
        _TRADE,
        None,
    ),
    "CashTransaction": _spec(
        "ibkr.cash_transaction",
        "accountId currency transactionID reportDate dateTime amount type",
        _CASH_TRANSACTION,
        "DETAIL",
    ),
    "ConversionRate": _spec(
        "ibkr.conversion_rate", "reportDate fromCurrency toCurrency rate", _CONVERSION_RATE, None
    ),
    "CorporateAction": _spec(
        "ibkr.corporate_action",
        "accountId currency transactionID reportDate dateTime quantity proceeds",
        _CORPORATE_ACTION,
        None,
    ),
    "Transfer": _spec(
        "ibkr.transfer",
        "accountId currency transactionID reportDate dateTime quantity",
        _TRANSFER,
        None,
    ),
    "EquitySummaryByReportDateInBase": _spec(
        "ibkr.equity_summary", "accountId currency reportDate total", _EQUITY_SUMMARY, None
    ),
}

# Sections whose single row is the section element itself, its fields being its attributes.
SINGLE_ROW_SECTIONS = frozenset({"AccountInformation"})
