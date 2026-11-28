# M7 Binance read-only REST endpoints: field catalogue

Compiled 2026-11-23 for the M7 design (`2026-11-22-binance-design.md`) and its implementation plan. Where a real response differs from this catalogue, the response wins and the catalogue is corrected.

Sources:
- Binance OpenAPI schemas, `https://developers.binance.com/en/docs/catalog/<product>/api/rest-api/1.0.0/schema.yaml`, read on 2026-11-23 (24 products: spot, wallet, margin, convert, USDⓈ-M futures, COIN-M futures, options, portfolio margin, portfolio margin pro, staking, mining, crypto loan, VIP loan, C2C, fiat, gift card, rebate, simple earn, discount buy, dual investment, pay, sub-account, copy trading, algo trading). Every one is version `1.0.0`.
- The owner-account probe summary (`private/m7-probe/summary*.txt`, shapes and counts only). No value from `raw/` was read.

Conventions:
- **Type** is the JSON type the schema documents: `string`, `number`, `integer`, `boolean`, `object`, `array`. **`decimal string`** = an amount, price or rate that arrives as a JSON string (store as text/decimal, never float). Where the schema says `number` but the schema's own example is a quoted string, the type column says `number (example is a string)`.
- Paths are dotted, `[]` marks an array level. A leading `[]` means the response body itself is an array.
- **Meaning** is the schema's description where it has one; otherwise a short gloss from the field name and the doc example. Enum values are listed only where the schema or the endpoint description lists them.
- **Seen** = "seen in the owner's data": `yes` (records returned; the real shape was compared), `empty` (HTTP 200, no record, so no real shape), `refused` (an error code), `not probed`.
- Unless a row says otherwise: host `api.binance.com`, signed (`USER_DATA`), every signed request also takes `timestamp` (required) and `recvWindow` (max 60000), all times are epoch milliseconds (UTC).
- Weight rule: `IP` = counted per IP (SAPI: 12,000/min per endpoint; `/api`: 6,000/min shared), `UID` = counted per account (SAPI: 180,000/min per endpoint).
- "Doc" gives the anchor under `developers.binance.com/en/docs/catalog/`.

Endpoint count: 111 endpoints with a field table (the probe's 85 + 22 added + `klines`, which the prices need, + the two futures hosts' and the options host's `exchangeInfo`, which their rate limits need), 19 more listed by one line. See "Index of labels" and "Totals" at the end.

---

## 1. Public (unsigned)

### `time` — GET /api/v3/time
Host api.binance.com · weight 1 (IP, `/api` pool) · unsigned · doc `core-trading-spot-trading/api/rest-api/general#time`
Request: none. Id: none. Time: `serverTime`. Seen: yes.

| Field | Type | Meaning |
|---|---|---|
| serverTime | integer | server time, epoch ms |

### `exchange-info` — GET /api/v3/exchangeInfo
Host api.binance.com · weight 20 (IP, `/api` pool) · unsigned · doc `core-trading-spot-trading/api/rest-api/general#exchange-info`
Request: all optional. `symbol`, `symbols` (JSON array text), `permissions`, `showPermissionSets` (default true), `symbolStatus` (enum TRADING, HALT, BREAK, CANCEL_ONLY; not combinable with `symbol`/`symbols`). No time or paging parameters.
Id: `symbols[].symbol`. Time: `serverTime`. Seen: yes.

| Field | Type | Meaning / enum |
|---|---|---|
| timezone | string | always "UTC" in the example |
| serverTime | integer | epoch ms |
| rateLimits[] | array | one object per limit |
| rateLimits[].rateLimitType | string | example REQUEST_WEIGHT (enum not listed in the schema) |
| rateLimits[].interval | string | example MINUTE (enum not listed in the schema) |
| rateLimits[].intervalNum | integer | interval multiplier |
| rateLimits[].limit | integer | limit value |
| rateLimits[].count | integer | in the schema, no description |
| exchangeFilters[] | array | oneOf by `filterType` |
| exchangeFilters[].filterType | string | EXCHANGE_MAX_NUM_ORDERS, EXCHANGE_MAX_NUM_ALGO_ORDERS, EXCHANGE_MAX_NUM_ICEBERG_ORDERS, EXCHANGE_MAX_NUM_ORDER_LISTS |
| exchangeFilters[].maxNumOrders / maxNumAlgoOrders / maxNumIcebergOrders / maxNumOrderLists | integer | one of them, by filterType |
| symbols[].symbol | string | pair name |
| symbols[].status | string | TRADING, HALT, BREAK, CANCEL_ONLY (request enum; real data showed TRADING and BREAK) |
| symbols[].baseAsset | string | base asset |
| symbols[].baseAssetPrecision | integer | decimals |
| symbols[].quoteAsset | string | quote asset |
| symbols[].quotePrecision | integer | "will be removed in future api versions (v4+)" |
| symbols[].quoteAssetPrecision | integer | decimals |
| symbols[].baseCommissionPrecision | integer | decimals |
| symbols[].quoteCommissionPrecision | integer | decimals |
| symbols[].orderTypes[] | string | allowed order types |
| symbols[].icebergAllowed, ocoAllowed, otoAllowed, opoAllowed, quoteOrderQtyMarketAllowed, allowTrailingStop, cancelReplaceAllowed, amendAllowed, pegInstructionsAllowed, marketQuoteNotionalLimitAllowed, limitQuoteNotionalLimitAllowed, isSpotTradingAllowed, isMarginTradingAllowed | boolean | 13 capability flags, one field each |
| symbols[].filters[] | array | oneOf by `filterType` (16 kinds, below) |
| symbols[].filters[].filterType | string | PRICE_FILTER, PERCENT_PRICE, PERCENT_PRICE_BY_SIDE, LOT_SIZE, MIN_NOTIONAL, NOTIONAL, ICEBERG_PARTS, MARKET_LOT_SIZE, MAX_NUM_ORDERS, MAX_NUM_ALGO_ORDERS, MAX_NUM_ICEBERG_ORDERS, MAX_POSITION, TRAILING_DELTA, T_PLUS_SELL, MAX_NUM_ORDER_LISTS, MAX_NUM_ORDER_AMENDS |
| …filters[] PRICE_FILTER | — | priceExponent integer; minPrice, maxPrice, tickSize decimal string |
| …filters[] PERCENT_PRICE | — | multiplierExponent integer; multiplierUp, multiplierDown decimal string; avgPriceMins integer |
| …filters[] PERCENT_PRICE_BY_SIDE | — | multiplierExponent integer; bidMultiplierUp, bidMultiplierDown, askMultiplierUp, askMultiplierDown decimal string; avgPriceMins integer |
| …filters[] LOT_SIZE, MARKET_LOT_SIZE | — | qtyExponent integer; minQty, maxQty, stepSize decimal string |
| …filters[] MIN_NOTIONAL | — | priceExponent integer; minNotional decimal string; applyToMarket boolean; avgPriceMins integer |
| …filters[] NOTIONAL | — | priceExponent integer; minNotional, maxNotional decimal string; applyMinToMarket, applyMaxToMarket boolean; avgPriceMins integer |
| …filters[] ICEBERG_PARTS | — | limit integer |
| …filters[] MAX_NUM_ORDERS / MAX_NUM_ALGO_ORDERS / MAX_NUM_ICEBERG_ORDERS / MAX_NUM_ORDER_LISTS / MAX_NUM_ORDER_AMENDS | — | maxNumOrders / maxNumAlgoOrders / maxNumIcebergOrders / maxNumOrderLists / maxNumOrderAmends integer |
| …filters[] MAX_POSITION | — | qtyExponent integer; maxPosition decimal string |
| …filters[] TRAILING_DELTA | — | minTrailingAboveDelta, maxTrailingAboveDelta, minTrailingBelowDelta, maxTrailingBelowDelta integer |
| …filters[] T_PLUS_SELL | — | endTime integer (epoch ms) |
| symbols[].permissions[] | string | permission names (no example value in the schema) |
| symbols[].permissionSets[][] | string | array of arrays of permission names (example SPOT); inner array = any of, outer array = all of |
| symbols[].defaultSelfTradePreventionMode | string | e.g. NONE |
| symbols[].allowedSelfTradePreventionModes[] | string | STP modes |
| sors[] | array | "Optional field. Present only when SOR is available." |
| sors[].baseAsset | string | base asset |
| sors[].symbols[] | string | pairs in the SOR |

For M7 only `symbol`, `status`, `baseAsset`, `quoteAsset` matter (to enumerate pairs for `my-trades`); the rest is trading-rule metadata.

---

### `klines` — GET /api/v3/klines
Host api.binance.com · weight 2 (IP, `/api` pool) · unsigned · doc `core-trading-spot-trading/api/rest-api/market-data-endpoints#klinecandlestick-data`
Request: `symbol` and `interval` required (M7 uses `1d`); `startTime`, `endTime` optional (epoch ms); `limit` default 500, max 1000; `timeZone` default 0 (UTC). Bars come oldest first.
Id: `symbol` (from the request) + open time. Time: open time. Seen: not probed. The body is an array of arrays; each bar is positional, so the paths below are positions.

| Field | Type | Meaning |
|---|---|---|
| [][0] | integer | open time, epoch ms |
| [][1] | decimal string | open price |
| [][2] | decimal string | high price |
| [][3] | decimal string | low price |
| [][4] | decimal string | close price |
| [][5] | decimal string | base asset volume |
| [][6] | integer | close time, epoch ms (23:59:59.999 UTC for a `1d` bar) |
| [][7] | decimal string | quote asset volume |
| [][8] | integer | number of trades |
| [][9] | decimal string | taker buy base asset volume |
| [][10] | decimal string | taker buy quote asset volume |
| [][11] | string | unused |

## 2. Key and account (Wallet REST API)

### `api-restrictions` — GET /sapi/v1/account/apiRestrictions
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/account#get-api-key-permission`
Request: none. Id: none (one object per key). Time: `createTime`. Seen: yes — exactly the 13 documented fields, no extra, none missing.

| Field | Type | Meaning |
|---|---|---|
| ipRestrict | boolean | IP access restriction on |
| createTime | integer | key creation, epoch ms |
| enableReading | boolean | read permission |
| enableWithdrawals | boolean | withdraw via API (needs the IP restriction) |
| enableInternalTransfer | boolean | master ↔ sub-account instant transfer |
| enableMargin | boolean | margin (adjustable after the cross-margin transfer is completed) |
| enableFutures | boolean | futures API |
| permitsUniversalTransfer | boolean | dedicated universal-transfer API |
| enableVanillaOptions | boolean | vanilla options trading |
| enableFixApiTrade | boolean | FIX API trading (no description in the schema) |
| enableFixReadOnly | boolean | FIX API read-only (no description in the schema) |
| enableSpotAndMarginTrading | boolean | spot and margin trading |
| enablePortfolioMarginTrading | boolean | portfolio margin API |

### `account-info` — GET /sapi/v1/account/info
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/account#account-info`
Request: none. Id/time: none. Seen: yes — same 5 fields. `vipLevel` documented integer, real is a JSON number with no decimals.

| Field | Type | Meaning |
|---|---|---|
| vipLevel | integer | VIP tier |
| isMarginEnabled | boolean | margin enabled |
| isFutureEnabled | boolean | futures enabled |
| isOptionsEnabled | boolean | options enabled |
| isPortfolioMarginRetailEnabled | boolean | portfolio margin retail enabled |

### `account-status` — GET /sapi/v1/account/status
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/account#account-status`
Request: none. Seen: yes.

| Field | Type | Meaning |
|---|---|---|
| data | string | account status text, e.g. "Normal" (enum not listed) |

### `api-trading-status` — GET /sapi/v1/account/apiTradingStatus
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/account#account-api-trading-status`
Request: none. Time: `data.updateTime`. Seen: yes — same fields.

| Field | Type | Meaning |
|---|---|---|
| data | object | wrapper |
| data.isLocked | boolean | API trading locked or not |
| data.plannedRecoverTime | integer | planned recovery time if locked, else 0 |
| data.triggerCondition | object | thresholds |
| data.triggerCondition.GCR | integer | number of GTC orders |
| data.triggerCondition.IFER | integer | number of FOK/IOC orders |
| data.triggerCondition.UFR | integer | number of orders |
| data.updateTime | integer | epoch ms (0 when never updated) |

---

## 3. Balances per wallet

### `spot-account` — GET /api/v3/account
Weight 20 (IP, `/api` pool) · signed · doc `core-trading-spot-trading/api/rest-api/account#get-account`
Request: `omitZeroBalances` (boolean, default false). No time or paging.
Id: `uid` (account), `balances[].asset` per row. Time: `updateTime`. Seen: yes — all 24 documented fields present, none extra. Real `permissions[]` value was `TRD_GRP_<n>`; the list contains LD-prefixed assets (Earn mirror).

| Field | Type | Meaning |
|---|---|---|
| makerCommission, takerCommission, buyerCommission, sellerCommission | integer | commission, 4 fields (unit not stated in the schema) |
| commissionRates | object | wrapper |
| commissionRates.maker, .taker, .buyer, .seller | decimal string | commission rates (4 fields) |
| canTrade, canWithdraw, canDeposit | boolean | account capabilities |
| brokered | boolean | brokered account |
| requireSelfTradePrevention | boolean | STP required |
| preventSor | boolean | SOR disabled |
| updateTime | integer | last account update, epoch ms |
| accountType | string | "SPOT" |
| balances[].asset | string | asset symbol |
| balances[].free | decimal string | available |
| balances[].locked | decimal string | locked in orders |
| permissions[] | string | e.g. SPOT, TRD_GRP_… |
| uid | integer | account id |

### `funding-asset` — POST /sapi/v1/asset/get-funding-asset
Weight 1 (IP) · signed · **POST query** · doc `core-trading-wallet/api/rest-api/asset#funding-wallet` (page name "Funding Wallet (USER_DATA)", description "Query Funding Wallet")
Request (body/query): `asset` optional, `needBtcValuation` optional boolean. Documented scope: "Currently supports querying the following business assets: Binance Pay, Binance Card, Binance Gift Card, Stock Token".
Id: `[].asset`. Time: none. Seen: empty (0 assets).

| Field | Type | Meaning |
|---|---|---|
| [].asset | string | asset symbol |
| [].free | decimal string | available balance |
| [].locked | decimal string | locked asset |
| [].freeze | decimal string | frozen asset |
| [].withdrawing | decimal string | being withdrawn |
| [].btcValuation | decimal string | BTC value |

### `user-asset` — POST /sapi/v3/asset/getUserAsset
Weight 5 (IP) · signed · **POST query** · doc `core-trading-wallet/api/rest-api/asset#user-asset` (page name "User Asset (USER_DATA)", description "Get user assets, just for positive data.")
Request: `asset` optional ("If asset is blank, then query all positive assets user have"), `needBtcValuation` optional boolean.
Id: `[].asset`. Time: none. Seen: yes — same 7 fields. Does not list LD-prefixed assets.

| Field | Type | Meaning |
|---|---|---|
| [].asset | string | asset symbol |
| [].free | decimal string | available |
| [].locked | decimal string | locked |
| [].freeze | decimal string | frozen |
| [].withdrawing | decimal string | being withdrawn |
| [].ipoable | decimal string | usable for Launchpad/IPO |
| [].btcValuation | decimal string | BTC value |

### `wallet-balance` — GET /sapi/v1/asset/wallet/balance
Weight 60 (IP) · signed · doc `core-trading-wallet/api/rest-api/asset#query-user-wallet-balance`
Request: `quoteAsset` (default BTC), `needBalanceDetail` (boolean, default false).
Id: `[].walletName`. Time: none. Seen: yes — same fields; with `needBalanceDetail=true` every wallet carried `assetBalances`. An element of `assetBalances` always carries `asset`, `free`, `locked`, `freeze` and `withdrawing` (the four quantities a wallet counted from the list sums): the connector requires them. Real `walletName` values (not listed in the docs): Spot, Funding, Cross Margin, Isolated Margin, USDⓈ-M Futures, COIN-M Futures, Earn, Options, Trading Bots, Copy Trading.

| Field | Type | Meaning |
|---|---|---|
| [].activate | boolean | wallet activated |
| [].balance | decimal string | wallet value in `quoteAsset` (a valuation) |
| [].walletName | string | wallet name (values above) |
| [].assetBalances[] | array | only when `needBalanceDetail=true` |
| [].assetBalances[].asset | string | asset symbol |
| [].assetBalances[].assetName | string | asset full name |
| [].assetBalances[].free | decimal string | available |
| [].assetBalances[].locked | decimal string | locked |
| [].assetBalances[].freeze | decimal string | frozen |
| [].assetBalances[].withdrawing | decimal string | being withdrawn |
| [].assetBalances[].btcValuation | decimal string | value in the quote asset |

### `earn-account` — GET /sapi/v1/simple-earn/account
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/flexible-locked#simple-account`
Request: none. Id/time: none. Seen: yes — same 6 fields.

| Field | Type | Meaning |
|---|---|---|
| totalAmountInBTC, totalAmountInUSDT | decimal string | Simple Earn total value |
| totalFlexibleAmountInBTC, totalFlexibleAmountInUSDT | decimal string | flexible total value |
| totalLockedInBTC, totalLockedInUSDT | decimal string | locked total value |

### `earn-flexible-position` — GET /sapi/v1/simple-earn/flexible/position
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/flexible-locked#get-flexible-product-position`
Request: `asset`, `productId` optional; paging `current` (default 1), `size` (default 10, max 100).
Id: `rows[].productId` (one position per product). Time: none. Seen: yes.
**Doc vs real:** the docs list `yesterdayAirdropPercentageRate` and `airDropAsset`; the real row had neither. The docs type the `tierAnnualPercentageRate.<tier>` values as number; the real value is a decimal string. All other fields match.

| Field | Type | Meaning |
|---|---|---|
| rows[].totalAmount | decimal string | position amount |
| rows[].tierAnnualPercentageRate | object | map: tier label (e.g. "0-5BTC") → rate |
| rows[].tierAnnualPercentageRate.<tier> | number (real: decimal string) | tier APR |
| rows[].latestAnnualPercentageRate | decimal string | latest APR |
| rows[].yesterdayAirdropPercentageRate | decimal string | yesterday's airdrop rate (not in the real row) |
| rows[].asset | string | asset |
| rows[].airDropAsset | string | airdrop asset (not in the real row) |
| rows[].canRedeem | boolean | redeemable |
| rows[].collateralAmount | decimal string | amount pledged as collateral |
| rows[].productId | string | product id, e.g. "USDT001" |
| rows[].yesterdayRealTimeRewards | decimal string | yesterday's real-time rewards |
| rows[].cumulativeBonusRewards | decimal string | cumulative bonus-tier rewards |
| rows[].cumulativeRealTimeRewards | decimal string | cumulative real-time rewards |
| rows[].cumulativeTotalRewards | decimal string | cumulative total rewards |
| rows[].autoSubscribe | boolean | auto-subscribe on |
| total | integer | row count |

### `earn-locked-position` — GET /sapi/v1/simple-earn/locked/position
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/flexible-locked#get-locked-product-position`
Request: `asset`, `positionId`, `projectId` optional; paging `current` (default 1), `size` (default 10, max 100).
Id: `rows[].positionId`. Time: `rows[].purchaseTime`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | integer | locked position id |
| rows[].parentPositionId | integer | parent position id |
| rows[].projectId | string | locked project id, e.g. "Axs*90" |
| rows[].asset | string | locked asset |
| rows[].amount | decimal string | locked amount |
| rows[].purchaseTime | integer | subscription time, epoch ms |
| rows[].duration | string | lock period in days (digits as text) |
| rows[].accrualDays | string | accrued days (digits as text) |
| rows[].rewardAsset | string | earned asset |
| rows[].APY | decimal string | APY |
| rows[].rewardAmt | decimal string | earned amount |
| rows[].extraRewardAsset | string | reward asset of the extra staking type |
| rows[].extraRewardAPR | decimal string | APR of the extra staking type |
| rows[].estExtraRewardAmt | decimal string | extra rewards, distributed when the order expires |
| rows[].boostRewardAsset | string | boost reward asset |
| rows[].boostApr | decimal string | boost APR |
| rows[].totalBoostRewardAmt | decimal string | total boost rewards |
| rows[].nextPay | decimal string | next estimated reward payment |
| rows[].nextPayDate | integer | next reward payment date, epoch ms |
| rows[].payPeriod | string | payment cycle in days (digits as text) |
| rows[].redeemAmountEarly | decimal string | early redemption amount |
| rows[].rewardsEndDate | integer | reward accrual end, epoch ms |
| rows[].deliverDate | integer | redemption arrival time, epoch ms |
| rows[].redeemPeriod | string | redemption interval in days (digits as text) |
| rows[].redeemingAmt | decimal string | amount under redemption |
| rows[].redeemTo | string | "Redeem to Flexible product or Spot wallet"; example FLEXIBLE (enum not listed) |
| rows[].partialAmtDeliverDate | integer | arrival time of a partial redemption, epoch ms |
| rows[].canRedeemEarly | boolean | early redemption possible |
| rows[].canFastRedemption | boolean | fast redemption possible |
| rows[].autoSubscribe | boolean | auto staking on |
| rows[].type | string | "auto subscribe or normal"; example AUTO |
| rows[].status | string | example HOLDING (enum not listed) |
| rows[].canReStake | boolean | re-stake possible |
| total | integer | row count |

### `bfusd-account` — GET /sapi/v1/bfusd/account
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/bfusd#get-bfusd-account`
Request: none. Seen: yes — same 3 fields.

| Field | Type | Meaning |
|---|---|---|
| bfusdAmount | decimal string | BFUSD holding |
| usdtProfit | decimal string | profit paid in USDT |
| bfusdProfit | decimal string | profit paid in BFUSD |

### `rwusd-account` — GET /sapi/v1/rwusd/account
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/rwusd#get-rwusd-account`
Request: none. Seen: yes — same 2 fields.

| Field | Type | Meaning |
|---|---|---|
| rwusdAmount | decimal string | RWUSD holding |
| totalProfit | decimal string | total profit |

### `margin-account` — GET /sapi/v1/margin/account
Weight 10 (IP) · signed · doc `core-trading-margin-trading/api/rest-api/account#query-cross-margin-account-details`
Request: none. Id: `userAssets[].asset` per row. Time: none. Seen: yes.
**Doc vs real:** real has `transferEnabled` (boolean), which the docs do not list; the docs list `collateralMarginLevel` and `TotalCollateralValueInUSDT` (capital T), which the real response did not carry. Real wins: add `transferEnabled`, keep the two documented ones nullable.

| Field | Type | Meaning / enum |
|---|---|---|
| created | boolean | true = margin account created |
| borrowEnabled | boolean | borrowing enabled |
| marginLevel | decimal string | margin level |
| collateralMarginLevel | decimal string | collateral margin level (not in the real response) |
| totalAssetOfBtc | decimal string | total assets in BTC |
| totalLiabilityOfBtc | decimal string | total liabilities in BTC |
| totalNetAssetOfBtc | decimal string | net assets in BTC |
| TotalCollateralValueInUSDT | decimal string | total collateral value in USDT (not in the real response) |
| tradeEnabled | boolean | trading enabled |
| transferInEnabled | boolean | transfer in enabled |
| transferOutEnabled | boolean | transfer out enabled |
| transferEnabled | boolean | **real only**, not in the docs |
| accountType | string | MARGIN_1 = Cross Margin Classic, MARGIN_2 = Cross Margin Pro |
| userAssets[].asset | string | asset |
| userAssets[].borrowed | decimal string | borrowed |
| userAssets[].free | decimal string | available |
| userAssets[].interest | decimal string | accrued interest |
| userAssets[].locked | decimal string | locked |
| userAssets[].netAsset | decimal string | net asset |

### `margin-isolated-account` — GET /sapi/v1/margin/isolated/account
Weight 10 (IP) · signed · doc `core-trading-margin-trading/api/rest-api/account#query-isolated-margin-account-info`
Request: `symbols` optional (max 5 symbols, comma separated). Without `symbols` the three `total*OfBtc` fields are returned; no paging.
Id: `assets[].symbol`. Time: none. Seen: yes but `assets` empty (0 pairs); the three totals were present.

| Field | Type | Meaning / enum |
|---|---|---|
| assets[].baseAsset | object | base-asset leg |
| assets[].baseAsset.asset | string | asset |
| assets[].baseAsset.borrowEnabled | boolean | borrowing enabled |
| assets[].baseAsset.borrowed | decimal string | borrowed |
| assets[].baseAsset.free | decimal string | available |
| assets[].baseAsset.interest | decimal string | accrued interest |
| assets[].baseAsset.locked | decimal string | locked |
| assets[].baseAsset.netAsset | decimal string | net asset |
| assets[].baseAsset.netAssetOfBtc | decimal string | net asset in BTC |
| assets[].baseAsset.repayEnabled | boolean | repay enabled |
| assets[].baseAsset.totalAsset | decimal string | total asset |
| assets[].quoteAsset.* | — | same 10 fields as `baseAsset` for the quote leg |
| assets[].symbol | string | isolated pair |
| assets[].isolatedCreated | boolean | isolated account created |
| assets[].enabled | boolean | true-enabled, false-disabled |
| assets[].marginLevel | decimal string | margin level |
| assets[].marginLevelStatus | string | EXCESSIVE, NORMAL, MARGIN_CALL, PRE_LIQUIDATION, FORCE_LIQUIDATION |
| assets[].marginRatio | decimal string | margin ratio |
| assets[].indexPrice | decimal string | index price |
| assets[].liquidatePrice | decimal string | liquidation price |
| assets[].liquidateRate | decimal string | liquidation rate |
| assets[].tradeEnabled | boolean | trading enabled |
| totalAssetOfBtc | decimal string | total assets in BTC |
| totalLiabilityOfBtc | decimal string | total liabilities in BTC |
| totalNetAssetOfBtc | decimal string | net assets in BTC |

### `um-balance` — GET /fapi/v3/balance
Host **fapi.binance.com** · weight 5 (IP, fapi pool) · signed · doc `core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#futures-account-balance-v3`
Request: none. Id: `[].asset`. Time: `[].updateTime`. Seen: yes — same 9 fields.

| Field | Type | Meaning |
|---|---|---|
| [].accountAlias | string | unique account alias |
| [].asset | string | asset name |
| [].balance | decimal string | wallet balance |
| [].crossWalletBalance | decimal string | cross wallet balance |
| [].crossUnPnl | decimal string | unrealized PnL of cross positions |
| [].availableBalance | decimal string | available balance |
| [].maxWithdrawAmount | decimal string | maximum transferable/withdrawable amount |
| [].marginAvailable | boolean | usable as margin in multi-assets mode |
| [].updateTime | integer | last update, epoch ms (0 when never) |

### `um-account` — GET /fapi/v3/account
Host fapi.binance.com · weight 5 (IP, fapi pool) · signed · doc `…usd-s-m-futures/api/rest-api/account#account-information-v3`
Request: none. Id: `assets[].asset`, `positions[].symbol`+`positionSide`. Time: `assets[].updateTime`. Seen: yes — all 11 totals and 13 `assets[]` fields match; `positions` was empty (the account has no position), so its 10 fields rest on the docs.

| Field | Type | Meaning |
|---|---|---|
| totalInitialMargin | decimal string | total initial margin (USDT in single-asset mode, USD value in multi-assets mode) |
| totalMaintMargin | decimal string | total maintenance margin |
| totalWalletBalance | decimal string | total wallet balance |
| totalUnrealizedProfit | decimal string | total unrealized profit |
| totalMarginBalance | decimal string | total margin balance |
| totalPositionInitialMargin | decimal string | initial margin for positions |
| totalOpenOrderInitialMargin | decimal string | initial margin for open orders |
| totalCrossWalletBalance | decimal string | cross wallet balance |
| totalCrossUnPnl | decimal string | unrealized PnL of cross positions |
| availableBalance | decimal string | available balance |
| maxWithdrawAmount | decimal string | maximum transferable amount |
| assets[].asset | string | asset name |
| assets[].walletBalance | decimal string | wallet balance |
| assets[].unrealizedProfit | decimal string | unrealized profit |
| assets[].marginBalance | decimal string | margin balance |
| assets[].maintMargin | decimal string | maintenance margin |
| assets[].initialMargin | decimal string | total initial margin |
| assets[].positionInitialMargin | decimal string | initial margin for positions |
| assets[].openOrderInitialMargin | decimal string | initial margin for open orders |
| assets[].crossWalletBalance | decimal string | cross wallet balance |
| assets[].crossUnPnl | decimal string | unrealized PnL of cross positions |
| assets[].availableBalance | decimal string | available balance |
| assets[].maxWithdrawAmount | decimal string | maximum transferable amount |
| assets[].updateTime | integer | last update, epoch ms |
| positions[].symbol | string | symbol |
| positions[].positionSide | string | position side (example BOTH) |
| positions[].positionAmt | decimal string | position quantity |
| positions[].unrealizedProfit | decimal string | unrealized profit |
| positions[].isolatedMargin | decimal string | isolated margin |
| positions[].notional | decimal string | position notional value |
| positions[].isolatedWallet | decimal string | isolated wallet balance |
| positions[].initialMargin | decimal string | initial margin requirement |
| positions[].maintMargin | decimal string | maintenance margin requirement |
| positions[].updateTime | integer | last update, epoch ms |

### `um-positions` — GET /fapi/v3/positionRisk
Host fapi.binance.com · weight 5 (IP, fapi pool) · signed · doc `…usd-s-m-futures/api/rest-api/trade#position-information-v3`
Request: `symbol` optional. "Only symbol that has position or open orders will be returned."
Id: `[].symbol` + `[].positionSide`. Time: `[].updateTime`. Seen: empty (0 positions).

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | symbol |
| [].positionSide | string | position side (example BOTH) |
| [].positionAmt | decimal string | position amount, positive long, negative short |
| [].entryPrice | decimal string | entry price |
| [].breakEvenPrice | decimal string | break-even price |
| [].markPrice | decimal string | current mark price |
| [].unRealizedProfit | decimal string | unrealized profit (capital R here, unlike `um-account`) |
| [].liquidationPrice | decimal string | liquidation price |
| [].isolatedMargin | decimal string | isolated margin |
| [].notional | decimal string | notional value |
| [].marginAsset | string | margin asset |
| [].isolatedWallet | decimal string | isolated wallet (isolated position) |
| [].initialMargin | decimal string | initial margin at the current mark price |
| [].maintMargin | decimal string | maintenance margin |
| [].positionInitialMargin | decimal string | initial margin for positions |
| [].openOrderInitialMargin | decimal string | initial margin for open orders |
| [].adl | integer | auto-deleverage ranking |
| [].bidNotional | decimal string | "ignore" |
| [].askNotional | decimal string | "ignore" |
| [].updateTime | integer | update time, epoch ms |

### `cm-balance` — GET /dapi/v1/balance
Host **dapi.binance.com** · weight 1 (IP, dapi pool) · signed · doc `core-trading-derivatives-trading-coin-m-futures/api/rest-api/account#futures-account-balance`
Request: none. Id: `[].asset`. Time: `[].updateTime`. Seen: yes — same 8 fields.

| Field | Type | Meaning |
|---|---|---|
| [].accountAlias | string | unique account code |
| [].asset | string | asset name |
| [].balance | decimal string | account balance |
| [].withdrawAvailable | decimal string | available for withdrawal |
| [].crossWalletBalance | decimal string | wallet balance for crossed margin |
| [].crossUnPnl | decimal string | unrealized PnL of crossed positions |
| [].availableBalance | decimal string | available margin balance |
| [].updateTime | integer | update time, epoch ms |

### `cm-account` — GET /dapi/v1/account
Host dapi.binance.com · weight 5 (IP, dapi pool) · signed · doc `…coin-m-futures/api/rest-api/account#account-information`
Request: none. Id: `assets[].asset`; `positions[].symbol`+`positionSide`. Time: `updateTime`. Seen: yes.
**Doc vs real:** real `positions[]` carries `isolatedWallet` (decimal string), not in the docs. Everything else matches.

| Field | Type | Meaning |
|---|---|---|
| assets[].asset | string | asset name |
| assets[].walletBalance | decimal string | total wallet balance |
| assets[].unrealizedProfit | decimal string | unrealized PnL |
| assets[].marginBalance | decimal string | margin balance |
| assets[].maintMargin | decimal string | maintenance margin |
| assets[].initialMargin | decimal string | total initial margin at the latest mark price |
| assets[].positionInitialMargin | decimal string | positions' margin |
| assets[].openOrderInitialMargin | decimal string | open orders' initial margin |
| assets[].maxWithdrawAmount | decimal string | maximum amount for transfer out |
| assets[].crossWalletBalance | decimal string | wallet balance for crossed margin |
| assets[].crossUnPnl | decimal string | unrealized PnL of crossed positions |
| assets[].availableBalance | decimal string | available margin balance |
| assets[].updateTime | integer | update time, epoch ms |
| positions[].symbol | string | contract symbol, e.g. BTCUSD_201225 |
| positions[].positionAmt | decimal string | position amount (contracts) |
| positions[].initialMargin | decimal string | total initial margin |
| positions[].maintMargin | decimal string | maintenance margin |
| positions[].unrealizedProfit | decimal string | unrealized PnL |
| positions[].positionInitialMargin | decimal string | positions' margin |
| positions[].openOrderInitialMargin | decimal string | open orders' initial margin |
| positions[].leverage | string | leverage (digits as text) |
| positions[].isolated | boolean | isolated margin mode |
| positions[].positionSide | string | BOTH, LONG, SHORT (real rows: LONG, SHORT) |
| positions[].entryPrice | decimal string | entry price |
| positions[].breakEvenPrice | decimal string | break-even price |
| positions[].maxQty | decimal string | maximum quantity of base asset |
| positions[].updateTime | integer | update time, epoch ms |
| positions[].notionalValue | decimal string | notional value |
| positions[].isolatedWallet | decimal string | **real only**, not in the docs |
| canDeposit, canTrade, canWithdraw | boolean | account capabilities |
| feeTier | integer | fee tier |
| updateTime | integer | update time, epoch ms |

### `cm-positions` — GET /dapi/v1/positionRisk
Host dapi.binance.com · weight 1 (IP, dapi pool) · signed · doc `…coin-m-futures/api/rest-api/trade#position-information`
Request: `marginAsset`, `pair` optional; "if neither is sent, positions of all symbols with TRADING status will be returned" (zero amounts included). One-way mode shows BOTH only; hedge mode shows BOTH, LONG and SHORT.
Id: `[].symbol` + `[].positionSide`. Time: `[].updateTime`. Seen: yes.
**Doc vs real:** real carries `isolatedWallet` and `notionalValue` (decimal strings), not in the docs.

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | contract symbol |
| [].positionAmt | decimal string | position amount |
| [].entryPrice | decimal string | entry price |
| [].breakEvenPrice | decimal string | break-even price |
| [].markPrice | decimal string | mark price |
| [].unRealizedProfit | decimal string | unrealized profit |
| [].liquidationPrice | decimal string | liquidation price |
| [].leverage | string | leverage (digits as text) |
| [].maxQty | decimal string | maximum quantity of base asset |
| [].marginType | string | margin type (example and real value "cross") |
| [].isolatedMargin | decimal string | isolated margin amount |
| [].isAutoAddMargin | string | "true"/"false" as text |
| [].positionSide | string | BOTH, LONG, SHORT |
| [].updateTime | integer | update time, epoch ms |
| [].isolatedWallet | decimal string | **real only** |
| [].notionalValue | decimal string | **real only** |

### `eth-staking-account` — GET /sapi/v2/eth-staking/account
Weight 150 (IP) · signed · doc `investment-and-services-staking/api/rest-api/eth-staking#eth-staking-account`
Request: none. Seen: yes — same fields.

| Field | Type | Meaning |
|---|---|---|
| holdingInETH | decimal string | holdings valued in ETH |
| holdings.wbethAmount | decimal string | WBETH held |
| holdings.bethAmount | decimal string | BETH held |
| thirtyDaysProfitInETH | decimal string | 30-day profit in ETH |
| profit.amountFromWBETH | decimal string | profit from WBETH |
| profit.amountFromBETH | decimal string | profit from BETH |

### `sol-staking-account` — GET /sapi/v1/sol-staking/account
Weight 150 (IP) · signed · doc `investment-and-services-staking/api/rest-api/sol-staking#sol-staking-account`
Request: none. Seen: yes — same fields.

| Field | Type | Meaning |
|---|---|---|
| bnsolAmount | decimal string | BNSOL held |
| holdingInSOL | decimal string | holdings valued in SOL |
| thirtyDaysProfitInSOL | decimal string | 30-day profit in SOL |

### `sol-unclaimed-rewards` — GET /sapi/v1/sol-staking/sol/history/unclaimedRewards
Weight 150 (IP) · signed · doc `…staking/api/rest-api/sol-staking#get-unclaimed-rewards`
Request: none listed (the page's notes still describe `startTime`/`endTime`: span ≤ 3 months, default last 30 days). Id: `[].rewardsAsset`. Time: none. Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| [].amount | decimal string | unclaimed amount |
| [].rewardsAsset | string | reward asset |

### `onchain-account` — GET /sapi/v1/onchain-yields/account
Weight 50 (IP) · signed · doc `…staking/api/rest-api/on-chain-yields#on-chain-yields-account`
Request: none. Seen: yes — same 6 fields.

| Field | Type | Meaning |
|---|---|---|
| totalAmountInBTC, totalAmountInUSDT | decimal string | total value |
| totalFlexibleAmountInBTC, totalFlexibleAmountInUSDT | decimal string | flexible total |
| totalLockedInBTC, totalLockedInUSDT | decimal string | locked total |

### `onchain-locked-position` — GET /sapi/v1/onchain-yields/locked/position
Weight 50 (IP) · signed · doc `…staking/api/rest-api/on-chain-yields#get-on-chain-yields-locked-product-position`
Request: `asset`, `positionId`, `projectId` optional; paging `current` (default 1), `size` (default 10, max 100).
Id: `rows[].positionId`. Time: `rows[].purchaseTime`. Seen: empty.
Note: unlike Simple Earn locked, **every date here is typed string** (epoch ms as digits text) and `positionId` is a string.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | string | locked position id |
| rows[].projectId | string | locked project id, e.g. "Solv-60d" |
| rows[].asset | string | locked asset |
| rows[].amount | decimal string | locked amount |
| rows[].purchaseTime | string | subscription time, epoch ms as text |
| rows[].duration | string | lock period (days) |
| rows[].accrualDays | string | accrued days |
| rows[].rewardAsset | string | earned asset |
| rows[].APY | decimal string | APY |
| rows[].rewardAmt | decimal string | earned amount |
| rows[].nextPay | decimal string | next estimated rewards payment |
| rows[].nextPayDate | string | next rewards payment date, epoch ms as text |
| rows[].payPeriod | string | payment cycle |
| rows[].rewardsPayDate | string | rewards payment date at maturity, epoch ms as text |
| rows[].rewardsEndDate | string | rewards accrual end, epoch ms as text |
| rows[].deliverDate | string | redemption arrival time; absent when auto-subscribe is on |
| rows[].nextSubscriptionDate | string | present when auto-subscribe is on |
| rows[].redeemingAmt | decimal string | amount under redemption |
| rows[].redeemTo | string | FLEXIBLE (Simple Earn flexible) or SPOT |
| rows[].canRedeemEarly | boolean | early redemption possible |
| rows[].autoSubscribe | boolean | auto subscribe |
| rows[].type | string | AUTO, NORMAL |
| rows[].status | string | HOLDING, REDEEMING, RENEWING (auto subscribing), NEW_TRANSFERRING (redeeming to Simple Earn flexible) |
| total | integer | row count |

### `soft-staking-list` — GET /sapi/v1/soft-staking/list
Weight 50 (IP) · signed · doc `…staking/api/rest-api/soft-staking#get-soft-staking-product-list`
Request: `asset` optional; paging `current` (default 1), `size` (default 10, max 100).
Id: `rows[].asset`. Time: none. Seen: yes — the 6 row fields match (the probe summarised `rows` only).

| Field | Type | Meaning |
|---|---|---|
| status | boolean | soft staking switched on for the account |
| totalRewardsUsdt | decimal string | total rewards in USDT |
| rows[].asset | string | asset |
| rows[].minAmount | decimal string | minimum holding |
| rows[].maxCap | decimal string | maximum cap |
| rows[].apr | decimal string | APR |
| rows[].stakedAmount | decimal string | amount counted as staked |
| rows[].totalProfit | decimal string | total profit |
| total | integer | row count |

### `dci-accounts` — GET /sapi/v1/dci/product/accounts
Weight 1 (IP) · signed · doc `investment-and-services-dual-investment/api/rest-api/trade#check-dual-investment-accounts`
Request: none. Seen: yes — same 2 fields.

| Field | Type | Meaning |
|---|---|---|
| totalAmountInBTC | decimal string | total BTC amount in Dual Investment |
| totalAmountInUSDT | decimal string | total USDT equivalent |

### `dci-positions` — GET /sapi/v1/dci/product/positions
Weight 1 (IP) · signed · doc `…dual-investment/api/rest-api/trade#get-dual-investment-positions`
Request: `status` optional (enum below); paging `pageIndex` (default 1), `pageSize` (default 10, max 100). No time window parameters.
Id: `list[].id` (position id); `list[].orderId` also present. Time: `list[].subscriptionTime`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| total | integer | row count |
| list[].id | string | position id |
| list[].investCoin | string | invested asset |
| list[].exercisedCoin | string | asset received if exercised |
| list[].subscriptionAmount | decimal string | invested amount |
| list[].strikePrice | decimal string | strike price |
| list[].duration | integer | days |
| list[].settleDate | integer | settlement date, epoch ms |
| list[].purchaseStatus | string | PENDING, PURCHASE_SUCCESS, SETTLED, PURCHASE_FAIL, REFUNDING, REFUND_SUCCESS, SETTLING |
| list[].apr | decimal string | APR |
| list[].orderId | integer | order id |
| list[].purchaseEndTime | integer | purchase end, epoch ms |
| list[].optionType | string | example PUT (enum not listed) |
| list[].autoCompoundPlan | string | NULL, STANDARD, ADVANCED |
| list[].subscriptionTime | integer | subscription time, epoch ms |

### `discount-accounts` — GET /sapi/v1/accumulator/product/sum-holding
Weight 1 (IP) · signed · doc `investment-and-services-discount-buy/api/rest-api/trade#check-discount-buy-accounts`
Request: none. Seen: yes — same 2 fields.

| Field | Type | Meaning |
|---|---|---|
| totalAmountInBTC | decimal string | total BTC amount in Discount Buy |
| totalAmountInUSDT | decimal string | total USDT equivalent |

### `discount-positions` — GET /sapi/v1/accumulator/product/position/list
Weight 1 (IP) · signed · doc `…discount-buy/api/rest-api/trade#get-discount-buy-positions`
Request: **`pageSize` required** (min 10, max 100), **`pageIndex` required** (default 1); `status` optional (same enum as below). No time window.
Id: `list[].id`. Time: **none documented** (only `settleDate`). Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| total | integer | row count |
| list[].id | string | position id |
| list[].investAsset | string | invested asset |
| list[].targetAsset | string | asset to accumulate |
| list[].depositAmount | decimal string | deposited amount |
| list[].strikePrice | decimal string | strike price |
| list[].knockoutPrice | decimal string | knock-out price |
| list[].duration | integer | days |
| list[].settleDate | integer | settlement date, epoch ms |
| list[].purchaseStatus | string | PENDING, PURCHASE_SUCCESS, SETTLED, PURCHASE_FAIL, REFUNDING, REFUND_SUCCESS, SETTLING |
| list[].knockoutApr | decimal string | knock-out APR |
| list[].orderId | integer | order id |

### `loan-flexible-ongoing` — GET /sapi/v2/loan/flexible/ongoing/orders
Weight 300 (IP) · signed · doc `investment-and-services-crypto-loan/api/rest-api/flexible-rate#get-flexible-loan-ongoing-orders`
Request: `loanCoin`, `collateralCoin` optional; paging `current` (default 1, max 1000), `limit` (default 10, max 100). Also "10 requests per second per account".
Id: none; `loanCoin` + `collateralCoin` identify a row. Time: none. Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| rows[].loanCoin | string | borrowed asset |
| rows[].totalDebt | decimal string | outstanding debt |
| rows[].collateralCoin | string | collateral asset |
| rows[].collateralAmount | decimal string | collateral amount |
| rows[].currentLTV | decimal string | current loan-to-value |
| total | integer | row count |

### `snapshot` — GET /sapi/v1/accountSnapshot
Weight 2400 (IP) · signed · doc `core-trading-wallet/api/rest-api/account#daily-account-snapshot`
Request: **`type` required** (SPOT, MARGIN, FUTURES); `startTime`, `endTime` optional; `limit` default 7, min 7, max 30. Documented: "The query time period must be less then 30 days", "Support query within the last one month only", default last 7 days when no time is sent. Real: 30 days returned, each at 23:59:59 UTC.
Id: `snapshotVos[].type` + `snapshotVos[].updateTime`. Time: `snapshotVos[].updateTime`.
Seen: SPOT yes (30 days); FUTURES empty (HTTP 200, code 200, 0 days); MARGIN refused (-3003).
Doc and real agree: the response `type` is lower-case (`spot`) although the request enum is upper-case; `code` 200 at top level; SPOT rows carry only `balances` and `totalAssetOfBtc`. An element of `data.balances` always carries `asset`, `free` and `locked`: the connector requires them.

| Field | Type | Meaning |
|---|---|---|
| code | integer | 200 for success; others are error codes |
| msg | string | error message |
| snapshotVos[].type | string | lower case; example and real value "spot" |
| snapshotVos[].updateTime | integer | snapshot time, epoch ms |
| snapshotVos[].data | object | content depends on `type` |
| SPOT: snapshotVos[].data.balances[].asset | string | asset |
| SPOT: snapshotVos[].data.balances[].free | decimal string | available |
| SPOT: snapshotVos[].data.balances[].locked | decimal string | locked |
| SPOT + MARGIN: snapshotVos[].data.totalAssetOfBtc | decimal string | total in BTC |
| MARGIN: snapshotVos[].data.marginLevel | decimal string | margin level |
| MARGIN: snapshotVos[].data.totalLiabilityOfBtc | decimal string | liabilities in BTC |
| MARGIN: snapshotVos[].data.totalNetAssetOfBtc | decimal string | net assets in BTC |
| MARGIN: snapshotVos[].data.userAssets[].asset | string | asset |
| MARGIN: snapshotVos[].data.userAssets[].borrowed, .free, .interest, .locked, .netAsset | decimal string | 5 amount fields |
| FUTURES: snapshotVos[].data.assets[].asset | string | asset |
| FUTURES: snapshotVos[].data.assets[].marginBalance | decimal string | "Not real-time data, can ignore" |
| FUTURES: snapshotVos[].data.assets[].walletBalance | decimal string | wallet balance |
| FUTURES: snapshotVos[].data.position[].entryPrice | decimal string | entry price |
| FUTURES: snapshotVos[].data.position[].markPrice | decimal string | mark price |
| FUTURES: snapshotVos[].data.position[].positionAmt | decimal string | position amount |
| FUTURES: snapshotVos[].data.position[].symbol | string | symbol |
| FUTURES: snapshotVos[].data.position[].unRealizedProfit | decimal string | "Only show the value at the time of opening the position" |

---

## 4. Deposits, withdrawals, fiat

### `deposits` — GET /sapi/v1/capital/deposit/hisrec
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/capital#deposit-history`
Request: all optional. `coin`, `status` (enum below), `txId`, `includeSource` (boolean, default false: "return `sourceAddress` field when set to true"). Window `startTime`/`endTime`: "must be less than 90 days"; default start = 90 days ago, default end = now. Paging `offset` (default 0), `limit` (default 1000, max 1000). No overall lookback documented; real: walked back to July 2017 with no refusal.
Id: `id` (string of digits). Time: `insertTime` (created), `completeTime` (credited). Seen: yes.
**Doc vs real:** real has `sourceAddress` (string) with `includeSource=true`; the response schema does not list it (only the parameter description does). Everything else matches; `status`, `transferType`, `walletType`, `unlockConfirm`, `travelRuleStatus` arrive as JSON numbers.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | string | deposit id (19 digits in real data) |
| [].amount | decimal string | deposited amount |
| [].coin | string | asset |
| [].network | string | network, e.g. BSC |
| [].status | integer | 0 pending, 1 success, 2 rejected, 6 credited but cannot withdraw, 7 wrong deposit, 8 waiting user confirm |
| [].address | string | deposit address |
| [].addressTag | string | memo/tag, may be empty |
| [].txId | string | chain transaction id (real: 0x + 64 hex); internal transfers carry an "Off-chain transfer …" text (notes file) |
| [].insertTime | integer | created, epoch ms |
| [].completeTime | integer | "deposit completion datetime, available for deposits after 6-Mar-2025" |
| [].transferType | integer | no description on this endpoint; withdraw history says 1 internal, 0 external |
| [].confirmTimes | string | "n/m" confirmations text, e.g. "1/1" |
| [].unlockConfirm | integer | confirmations needed to unlock |
| [].walletType | integer | no description here; withdraw history says 0 spot, 1 funding |
| [].travelRuleStatus | integer | 0 travel rule not required or info already provided, 1 info required |
| [].sourceAddress | string | **real only** (with `includeSource=true`) |

### `withdrawals` — GET /sapi/v1/capital/withdraw/history
Weight **18000 (UID)**, "10 requests per second" · signed · doc `core-trading-wallet/api/rest-api/capital#withdraw-history`
Request: all optional. `coin`, `withdrawOrderId`, `status`, `idList` (max 45 ids). Window `startTime`/`endTime`: "must be less than 90 days" (7 days when `withdrawOrderId` is sent); default last 90 days. Paging `offset` (default 0), `limit` (default 1000, max 1000). No overall lookback documented; real: walked back to July 2017.
Id: `id` (32-char text). Time: `applyTime` and `completeTime`, both **text** "YYYY-MM-DD HH:MM:SS", documented as UTC. Seen: yes.
**Doc vs real:** matches. `withdrawOrderId` absent in both real rows (documented: "will not be returned if there's no withdrawOrderId"); "`network` may not be in the response for old withdraw". No `addressTag` in docs or real.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | string | withdrawal id in Binance |
| [].amount | decimal string | withdrawal amount (fee not included) |
| [].transactionFee | decimal string | transaction fee |
| [].coin | string | asset |
| [].status | integer | 0 Email Sent, 1 Cancelled, 2 Awaiting Approval, 3 Rejected, 4 Processing, 5 Failure, 6 Completed (the request parameter lists 0, 2, 3, 4, 6; 1 and 5 come from Binance's response documentation) |
| [].address | string | destination address |
| [].txId | string | withdrawal transaction id (real: 0x+64 hex or bare 64 hex) |
| [].applyTime | string | UTC time text, "2019-10-12 11:12:02" |
| [].network | string | network; may be missing on old withdrawals |
| [].transferType | integer | 1 internal transfer, 0 external transfer |
| [].withdrawOrderId | string | client id; omitted when none |
| [].info | string | reason for withdrawal failure (real: set on completed rows too) |
| [].confirmNo | integer | confirmations |
| [].walletType | integer | 1 Funding Wallet, 0 Spot Wallet |
| [].txKey | string | may be empty |
| [].completeTime | string | UTC time text when the asset was deducted, "only if status = 6" |

### `travel-deposits` — GET /sapi/v1/localentity/deposit/history
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/travel-rule#deposit-history-travel-rule`
Request: all optional. `trId`, `txId`, `tranId` (comma lists), `network`, `coin`, `travelRuleStatus` (0 Completed, 1 Pending, 2 Failed), `pendingQuestionnaire`. Window `startTime`/`endTime` < 90 days, default last 90 days. Paging `offset`, `limit` (default 1000, max 1000).
Id: `trId` (travel-rule record) / `tranId` (wallet transfer). Time: `insertTime`. Seen: refused (-30105 "This endpoint is not supported for your account").
A V2 exists: GET /sapi/v2/localentity/deposit/history (weight 1 IP), not probed.

| Field | Type | Meaning / enum |
|---|---|---|
| [].trId | integer | travel rule record id |
| [].tranId | integer | wallet transfer id |
| [].amount | decimal string | amount |
| [].coin | string | asset |
| [].network | string | network |
| [].depositStatus | integer | deposit status (same codes as `deposits.status`, not restated) |
| [].travelRuleStatus | integer | 0 Completed, 1 Pending, 2 Failed (request parameter) |
| [].travelRuleStatusV2 | string | PASSED, PENDING, REJECTED |
| [].address | string | deposit address |
| [].addressTag | string | memo/tag |
| [].txId | string | transaction id |
| [].insertTime | integer | created, epoch ms |
| [].completeTime | integer | fully completed/credited, epoch ms |
| [].transferType | integer | internal/external flag |
| [].confirmTimes | string | "n/m" text |
| [].requireQuestionnaire | boolean | true = questionnaire needed before credit |
| [].questionnaire | string | JSON text of the answers |

### `travel-withdrawals` — GET /sapi/v1/localentity/withdraw/history
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/travel-rule#withdraw-history-v1`
Request: all optional. `trId`, `txId`, `withdrawOrderId`, `network`, `coin`, `travelRuleStatus`. Window < 90 days, default last 90 days. Paging `offset` (default 0), `limit` (default 1000, max 1000).
Id: `id`; `trId`. Time: `applyTime`, `completeTime` (UTC text, as `withdrawals`). Seen: refused (-30105). A V2 exists: GET /sapi/v2/localentity/withdraw/history, not probed.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | string | withdrawal id in Binance |
| [].trId | integer | travel rule record id |
| [].amount | decimal string | withdrawal amount |
| [].transactionFee | decimal string | fee |
| [].coin | string | asset |
| [].withdrawalStatus | integer | capital withdrawal status (codes as `withdrawals.status`) |
| [].travelRuleStatus | integer | travel rule status (0 Completed, 1 Pending, 2 Failed) |
| [].address | string | destination address |
| [].txId | string | transaction id |
| [].applyTime | string | UTC time text |
| [].network | string | network |
| [].transferType | integer | 1 internal, 0 external |
| [].withdrawOrderId | string | client id; omitted when none |
| [].info | string | failure reason |
| [].confirmNo | integer | confirmations |
| [].walletType | integer | 1 Funding, 0 Spot |
| [].txKey | string | — |
| [].questionnaire | string | JSON text of the answers |
| [].completeTime | string | UTC time text, only if status = 6 |

### `fiat-orders` — GET /sapi/v1/fiat/orders
Weight **45000 (UID)** · signed · doc `investment-and-services-fiat/api/rest-api/~#get-fiat-deposit-withdraw-history`
Request: **`transactionType` required** ("0: deposit, 1: withdraw"). Window `beginTime`/`endTime` (note the name: not `startTime`): no maximum documented; default "recent 30-day data". Paging `page` (default 1), `rows` (default 100, max 500). Real: one 3,432-day window was accepted (HTTP 200).
Id: `data[].orderNo`. Time: `data[].createTime` (`updateTime` also). Seen: empty for both types.

| Field | Type | Meaning / enum |
|---|---|---|
| code | string | "000000" on success |
| message | string | "success" |
| data[].orderNo | string | order number (UUID-like) |
| data[].fiatCurrency | string | fiat currency |
| data[].indicatedAmount | decimal string | trade amount |
| data[].amount | decimal string | actual amount after fee |
| data[].totalFee | decimal string | fee |
| data[].method | string | trade method, e.g. BankAccount |
| data[].status | string | Processing, Failed, Successful, Finished, Refunding, Refunded, Refund Failed, Order Partial Credit Stopped (the schema example shows "Expired", not in the list) |
| data[].createTime | integer | created, epoch ms |
| data[].updateTime | integer | updated, epoch ms |
| total | integer | row count |
| success | boolean | request success |

### `fiat-payments` — GET /sapi/v1/fiat/payments
Weight 1 (IP) · signed · doc `investment-and-services-fiat/api/rest-api/~#get-fiat-payments-history`
Request: **`transactionType` required** ("0: buy, 1: sell"). Window `beginTime`/`endTime`: no maximum documented; default recent 30 days. Paging `page` (default 1), `rows` (default 100, max 500).
Id: `data[].orderNo`. Time: `data[].createTime`. Seen: empty for both types.

| Field | Type | Meaning / enum |
|---|---|---|
| code | string | "000000" on success |
| message | string | "success" |
| data[].orderNo | string | order number |
| data[].sourceAmount | decimal string | fiat trade amount |
| data[].fiatCurrency | string | fiat currency |
| data[].obtainAmount | decimal string | crypto trade amount |
| data[].cryptoCurrency | string | crypto asset |
| data[].totalFee | decimal string | fee |
| data[].price | decimal string | price |
| data[].status | string | Processing, Completed, Failed, Refunded |
| data[].paymentMethod | string | Cash Balance, Credit Card, Online Banking, Bank Transfer; "returned only when querying buy history" |
| data[].createTime | integer | created, epoch ms |
| data[].updateTime | integer | updated, epoch ms |
| total | integer | row count |
| success | boolean | request success |

Also in the fiat product: GET /sapi/v1/fiat/get-order-detail (weight 1 IP), a single-order lookup; not catalogued.

---

## 5. Convert, transfers, distributions

### `convert` — GET /sapi/v1/convert/tradeFlow
Weight **3000 (UID)** · signed · doc `core-trading-convert/api/rest-api/trade#get-convert-trade-history`
Request: **`startTime` and `endTime` required**; "the max interval between startTime and endTime is 30 days". `limit` default 100, max 1000. No page parameter: when `moreData` is true, narrow the window. No overall lookback documented; real: walked back to July 2017 with no refusal.
Id: `list[].orderId` (integer); `quoteId` also unique per quote. Time: `list[].createTime`. Seen: yes.
**Doc vs real:** real rows also carry `orderType` (value MARKET), `side` (value SELL) and `walletType` (value SPOT); none of the three is in the docs. Real wins: three more columns.

| Field | Type | Meaning / enum |
|---|---|---|
| list[].quoteId | string | quote id |
| list[].orderId | integer | order id |
| list[].orderStatus | string | order status; enum not listed in the schema (example and real value SUCCESS) |
| list[].fromAsset | string | source asset |
| list[].fromAmount | decimal string | source amount |
| list[].toAsset | string | destination asset |
| list[].toAmount | decimal string | destination amount |
| list[].ratio | decimal string | price ratio |
| list[].inverseRatio | decimal string | inverse price ratio |
| list[].createTime | integer | created, epoch ms |
| list[].orderType | string | **real only**; seen MARKET |
| list[].side | string | **real only**; seen SELL |
| list[].walletType | string | **real only**; seen SPOT |
| startTime | integer | echo of the request |
| endTime | integer | echo of the request |
| limit | integer | echo of the request |
| moreData | boolean | more rows exist in the window than `limit` |

### `universal-transfer` — GET /sapi/v1/asset/transfer
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/asset#query-user-universal-transfer-history`
Request: **`type` required** (one type per request, enum below). `startTime`/`endTime` optional: no per-query maximum documented; "Support query within the last 6 months only"; default last 7 days. Paging `current` (default 1), `size` (default 10, max 100). `fromSymbol` must be sent for `ISOLATEDMARGIN_MARGIN` and `ISOLATEDMARGIN_ISOLATEDMARGIN`; `toSymbol` for `MARGIN_ISOLATEDMARGIN` and `ISOLATEDMARGIN_ISOLATEDMARGIN`.
Id: `rows[].tranId`. Time: `rows[].timestamp`. Seen: empty for all 28 probed types; a window older than 6 months was refused (-5026 "Start time query records range is too large"). The 3 isolated-margin types were not probed (they need a symbol).
Type enum (31, from the POST page of the same path): MAIN_UMFUTURE, MAIN_CMFUTURE, MAIN_MARGIN, UMFUTURE_MAIN, UMFUTURE_MARGIN, CMFUTURE_MAIN, CMFUTURE_MARGIN, MARGIN_MAIN, MARGIN_UMFUTURE, MARGIN_CMFUTURE, **ISOLATEDMARGIN_MARGIN** (isolated → cross), **MARGIN_ISOLATEDMARGIN** (cross → isolated), **ISOLATEDMARGIN_ISOLATEDMARGIN** (isolated → isolated), MAIN_FUNDING, FUNDING_MAIN, FUNDING_UMFUTURE, UMFUTURE_FUNDING, MARGIN_FUNDING, FUNDING_MARGIN, FUNDING_CMFUTURE, CMFUTURE_FUNDING, MAIN_OPTION, OPTION_MAIN, UMFUTURE_OPTION, OPTION_UMFUTURE, MARGIN_OPTION, OPTION_MARGIN, FUNDING_OPTION, OPTION_FUNDING, MAIN_PORTFOLIO_MARGIN, PORTFOLIO_MARGIN_MAIN. (MAIN = Spot, MARGIN = cross margin.)

| Field | Type | Meaning / enum |
|---|---|---|
| total | integer | row count |
| rows[].asset | string | asset |
| rows[].amount | decimal string | amount |
| rows[].type | string | transfer type (enum above) |
| rows[].status | string | CONFIRMED, FAILED, PENDING |
| rows[].tranId | integer | transfer id |
| rows[].timestamp | integer | epoch ms |

The response has **no symbol field**: for the three isolated types the pair is known only from the request's `fromSymbol`/`toSymbol`, so the connector must store the requested symbols with each row.

### `dividends` — GET /sapi/v1/asset/assetDividend
Weight 10 (IP) · signed · doc `core-trading-wallet/api/rest-api/asset#asset-dividend-record`
Request: `asset` optional. `startTime`/`endTime`: "There cannot be more than 180 days between"; default when omitted not documented. `limit` default 20, max 500; no page parameter (a full page means: split the window). No overall lookback documented; real: reached the first record (about 13 months).
Id: `rows[].id` (`tranId` also). Time: `rows[].divTime`. Seen: yes — same 7 fields; `direction` always 1; `enInfo` always "flexible".

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].id | integer | record id |
| rows[].amount | decimal string | amount |
| rows[].asset | string | asset |
| rows[].divTime | integer | distribution time, epoch ms |
| rows[].enInfo | string | free-text source, e.g. "BHFT distribution"; real "flexible" |
| rows[].tranId | integer | transaction id |
| rows[].direction | integer | 1 credited (inflow), -1 debited (outflow) |
| total | integer | row count |

### `dust` — GET /sapi/v1/asset/dribblet
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/asset#dustlog`
Request: `accountType` optional (SPOT, MARGIN); `startTime`/`endTime` optional, no maximum documented. "Only return last 100 records"; "Only return records after 2020/12/01". No paging.
Id: `userAssetDribblets[].transId` (one conversion); detail lines repeat the same `transId`, so a line is identified by `transId` + `fromAsset`. Time: `operateTime`. Seen: SPOT empty; MARGIN refused (-3003).

| Field | Type | Meaning |
|---|---|---|
| total | integer | total count of conversions |
| userAssetDribblets[].operateTime | integer | conversion time, epoch ms |
| userAssetDribblets[].totalTransferedAmount | decimal string | total BNB received in this conversion |
| userAssetDribblets[].totalServiceChargeAmount | decimal string | total service charge |
| userAssetDribblets[].transId | integer | conversion id |
| userAssetDribblets[].userAssetDribbletDetails[].transId | integer | conversion id (same as the parent) |
| userAssetDribblets[].userAssetDribbletDetails[].serviceChargeAmount | decimal string | service charge of this line |
| userAssetDribblets[].userAssetDribbletDetails[].amount | decimal string | amount of `fromAsset` converted |
| userAssetDribblets[].userAssetDribbletDetails[].operateTime | integer | epoch ms |
| userAssetDribblets[].userAssetDribbletDetails[].transferedAmount | decimal string | amount of `targetAsset` received |
| userAssetDribblets[].userAssetDribbletDetails[].fromAsset | string | dust asset |
| userAssetDribblets[].userAssetDribbletDetails[].targetAsset | string | asset received (example BNB) |

### `cloud-mining` — GET /sapi/v1/asset/ledger-transfer/cloud-mining/queryByPage
Weight **600 (UID)** · signed · doc `core-trading-wallet/api/rest-api/asset#get-cloud-mining-payment-and-refund-history`
Request: **`startTime` (inclusive) and `endTime` (exclusive) required**, no maximum span documented. `tranId`, `clientTranId`, `asset` optional. Paging `current` (default 1), `size` (default 10, max 100). "Just return the SUCCESS records of payment and refund."
Id: `rows[].tranId`. Time: `rows[].createTime`. Seen: refused — **HTTP 404 "No message available"** although the path is in the current docs. Treat as unavailable for this account.

| Field | Type | Meaning / enum |
|---|---|---|
| total | integer | row count |
| rows[].createTime | integer | epoch ms |
| rows[].tranId | integer | transaction id |
| rows[].type | integer | 248 payment, 249 refund |
| rows[].asset | string | asset |
| rows[].amount | decimal string | amount |
| rows[].status | string | "S" = SUCCESS |

### `coin-config` (not probed) — GET /sapi/v1/capital/config/getall
Weight 10 (IP) · signed · doc `core-trading-wallet/api/rest-api/capital#all-coins-information` ("Get information of coins (available for deposit and withdraw) for user.")
Request: none. Id: `[].coin`; a network row is `[].coin` + `networkList[].network`. Time: none. Seen: not probed.
Use: network names for `deposits.network` / `withdrawals.network`, and the per-network withdraw fee.

| Field | Type | Meaning |
|---|---|---|
| [].coin | string | asset symbol |
| [].depositAllEnable | boolean | deposits enabled on every network |
| [].withdrawAllEnable | boolean | withdrawals enabled on every network |
| [].name | string | asset full name |
| [].free | decimal string | the user's available balance |
| [].locked | decimal string | locked |
| [].freeze | decimal string | frozen |
| [].withdrawing | decimal string | being withdrawn |
| [].ipoing | decimal string | in Launchpad subscription |
| [].ipoable | decimal string | usable for Launchpad |
| [].storage | decimal string | storage amount |
| [].isLegalMoney | boolean | fiat currency |
| [].trading | boolean | tradable |
| [].networkList[].network | string | network code, e.g. BSC |
| [].networkList[].coin | string | asset symbol |
| [].networkList[].withdrawIntegerMultiple | decimal string | withdraw amount step |
| [].networkList[].isDefault | boolean | default network |
| [].networkList[].depositEnable | boolean | deposits enabled |
| [].networkList[].withdrawEnable | boolean | withdrawals enabled |
| [].networkList[].depositDesc | string | shown only when `depositEnable` is false |
| [].networkList[].withdrawDesc | string | shown only when `withdrawEnable` is false |
| [].networkList[].specialTips | string | deposit tips |
| [].networkList[].specialWithdrawTips | string | withdraw tips |
| [].networkList[].name | string | network full name, e.g. "BNB Smart Chain (BEP20)" |
| [].networkList[].resetAddressStatus | boolean | address reset flag |
| [].networkList[].addressRegex | string | address pattern |
| [].networkList[].memoRegex | string | memo pattern |
| [].networkList[].withdrawFee | decimal string | withdraw fee on this network |
| [].networkList[].withdrawMin | decimal string | minimum withdrawal |
| [].networkList[].withdrawMax | decimal string | maximum withdrawal |
| [].networkList[].withdrawInternalMin | decimal string | minimum internal transfer amount |
| [].networkList[].depositDust | decimal string | deposits below this are dust |
| [].networkList[].minConfirm | integer | confirmations for balance credit |
| [].networkList[].unLockConfirm | integer | confirmations for balance unlock |
| [].networkList[].sameAddress | boolean | obsolete; use `withdrawTag` |
| [].networkList[].withdrawTag | boolean | memo needed to withdraw |
| [].networkList[].estimatedArrivalTime | integer | estimated arrival time (unit not stated) |
| [].networkList[].busy | boolean | network busy |
| [].networkList[].contractAddressUrl | string | explorer URL prefix |
| [].networkList[].contractAddress | string | token contract address |
| [].networkList[].denomination | integer | units per coin, e.g. 1 1MBABYDOGE = 1000000 BABYDOGE |

### `asset-detail` (not probed, brief) — GET /sapi/v1/asset/assetDetail
Weight 1 (IP) · signed · doc `core-trading-wallet/api/rest-api/asset#asset-detail`. Request: `asset` optional. "Please get network and other deposit or withdraw details from GET /sapi/v1/capital/config/getall."
The body is a **map keyed by asset symbol** (`<ASSET>`), not an array.

| Field | Type | Meaning |
|---|---|---|
| <ASSET>.minWithdrawAmount | decimal string | minimum withdraw amount |
| <ASSET>.depositStatus | boolean | false if all networks are false |
| <ASSET>.withdrawFee | integer or number (two examples differ) | withdraw fee |
| <ASSET>.withdrawStatus | boolean | false if all networks are false |
| <ASSET>.depositTip | string | reason, present when deposits are suspended |

---

## 6. Simple Earn histories

Shared request rule for the six flexible/locked history endpoints and the collateral record: `startTime`/`endTime` optional, "cannot be longer than 30 days"; neither sent = last 30 days; only `startTime` = the next 30 days; only `endTime` = the 30 days before it. Paging `current` (default 1, starts at 1), `size` (default 10, max 100). No overall lookback documented; real: flexible histories reached the first record (about 13 months), 115 windows back to July 2017 all answered 200.

### `earn-flexible-subscriptions` — GET /sapi/v1/simple-earn/flexible/history/subscriptionRecord
Weight 150 (IP) · signed · doc `investment-and-services-simple-earn/api/rest-api/flexible-locked#get-flexible-subscription-record`
Request: `productId`, `purchaseId`, `asset` optional + the shared rule.
Id: `rows[].purchaseId`. Time: `rows[].time`. Seen: yes. `amtFromSpot`/`amtFromFunding` never appeared (no SPOTANDFUNDING row).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].amount | decimal string | subscribed amount |
| rows[].asset | string | asset |
| rows[].time | integer | epoch ms |
| rows[].purchaseId | integer | subscription id |
| rows[].productId | string | product id |
| rows[].type | string | AUTO (auto subscribe), NORMAL, CONVERT (Locked to Flexible), LOAN (flexible loan collateral), AI (Auto Invest), TRANSFER (Locked Savings to Flexible) |
| rows[].sourceAccount | string | SPOT, FUNDING, SPOTANDFUNDING |
| rows[].amtFromSpot | decimal string | only if sourceAccount is SPOTANDFUNDING |
| rows[].amtFromFunding | decimal string | only if sourceAccount is SPOTANDFUNDING |
| rows[].status | string | PURCHASING, SUCCESS, FAILED |
| total | integer | row count |

### `earn-flexible-redemptions` — GET /sapi/v1/simple-earn/flexible/history/redemptionRecord
Weight 150 (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-flexible-redemption-record`
Request: `productId`, `redeemId`, `asset` optional + the shared rule.
Id: `rows[].redeemId`. Time: `rows[].time`. Seen: yes.
**Doc vs real:** the docs name the product field `projectId`; the real field is **`productId`**. Real wins.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].amount | decimal string | redeemed amount |
| rows[].asset | string | asset |
| rows[].time | integer | epoch ms |
| rows[].productId | string | product id (the docs name it `projectId`; the real name is `productId`) |
| rows[].redeemId | integer | redemption id |
| rows[].destAccount | string | SPOT, FUNDING |
| rows[].status | string | CREATED, PAID, FAILED |
| total | integer | row count |

### `earn-flexible-rewards` — GET /sapi/v1/simple-earn/flexible/history/rewardsRecord
Weight 150 (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-flexible-rewards-history`
Request: `productId`, `asset` optional; `type` (BONUS = bonus tiered APR, REALTIME = real-time APR, REWARDS = historical rewards, ALL; "Default: ALL") + the shared rule.
Id: **none**. A record is identified by `asset` + `productId` + `type` + `time`. Time: `rows[].time`. Seen: yes.
**Doc vs real:** the docs name the product field `projectId`; the real field is **`productId`**. Real wins. The docs give `type` a default; the real endpoint refuses a request without it (HTTP 400, code -1102), so `type=ALL` is always sent.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].asset | string | asset |
| rows[].rewards | decimal string | reward amount |
| rows[].productId | string | product id (the docs name it `projectId`; the real name is `productId`) |
| rows[].type | string | BONUS, REALTIME, REWARDS |
| rows[].time | integer | epoch ms |
| total | integer | row count |

### `earn-flexible-collateral` (not probed) — GET /sapi/v1/simple-earn/flexible/history/collateralRecord
Weight **1** (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-collateral-record`
Request: `productId` optional + the shared rule.
Id: `rows[].orderId`. Time: `rows[].createTime`. Seen: not probed.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].amount | decimal string | collateral amount moved |
| rows[].productId | string | product id |
| rows[].asset | string | asset |
| rows[].createTime | integer | epoch ms |
| rows[].type | string | example REPAY (enum not listed) |
| rows[].productName | string | product name |
| rows[].orderId | integer | loan order id |
| total | **string** | row count as text ("1") — unlike the other histories |

### `earn-locked-subscriptions` — GET /sapi/v1/simple-earn/locked/history/subscriptionRecord
Weight 150 (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-locked-subscription-record`
Request: `purchaseId`, `asset` optional + the shared rule.
Id: `rows[].purchaseId` (a **string** here; integer on flexible). Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | integer | locked position id |
| rows[].purchaseId | string | subscription id |
| rows[].projectId | string | locked project id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | asset |
| rows[].amount | decimal string | subscribed amount |
| rows[].lockPeriod | string | lock period in days (digits as text) |
| rows[].type | string | NORMAL, AUTO, ACTIVITY, TRIAL (trial fund), RESTAKE |
| rows[].sourceAccount | string | SPOT, FUNDING, SPOTANDFUNDING |
| rows[].amtFromSpot | decimal string | only if SPOTANDFUNDING |
| rows[].amtFromFunding | decimal string | only if SPOTANDFUNDING |
| rows[].status | string | PURCHASING, SUCCESS, FAILED |
| total | integer | row count |

### `earn-locked-redemptions` — GET /sapi/v1/simple-earn/locked/history/redemptionRecord
Weight 150 (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-locked-redemption-record`
Request: `positionId`, `redeemId`, `asset` optional + the shared rule.
Id: `rows[].redeemId`. Time: `rows[].time` (`deliverDate` = arrival). Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | integer | locked position id |
| rows[].redeemId | integer | redemption id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | asset |
| rows[].lockPeriod | string | lock period in days |
| rows[].amount | decimal string | redeemed amount |
| rows[].originalAmount | decimal string | original locked amount |
| rows[].type | string | MATURE (to Spot), NEW_TRANSFERRED (to Flexible), AHEAD (early redemption) |
| rows[].deliverDate | integer | arrival, epoch ms |
| rows[].lossAmount | decimal string | profit lost on early redemption |
| rows[].isComplete | boolean | redemption complete |
| rows[].rewardAsset | string | reward asset |
| rows[].rewardAmt | decimal string | reward amount |
| rows[].extraRewardAsset | string | extra reward asset |
| rows[].estExtraRewardAmt | decimal string | extra reward amount |
| rows[].status | string | example PAID (enum not listed) |
| total | integer | row count |

### `earn-locked-rewards` — GET /sapi/v1/simple-earn/locked/history/rewardsRecord
Weight 150 (IP) · signed · doc `…simple-earn/api/rest-api/flexible-locked#get-locked-rewards-history`
Request: `positionId`, `asset` optional + the shared rule.
Id: **none**; `positionId` + `asset` + `type` + `time`. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | integer | locked position id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | reward asset |
| rows[].lockPeriod | string | lock period in days |
| rows[].amount | decimal string | reward amount |
| rows[].type | string | example "Locked Rewards" (enum not listed) |
| total | integer | row count |

### BFUSD and RWUSD histories (6 endpoints)
All: weight 150 (IP) · signed · docs `investment-and-services-simple-earn/api/rest-api/bfusd#…` and `…/rwusd#…`.
Shared request rule: `startTime`/`endTime` optional, documented "cannot be longer than 6 months"; neither sent = last 30 days; only `startTime` = from it to now; only `endTime` = the month before it. Paging `current` (default 1), `size` (default 10, max 100). Subscription histories also take `asset` (enum USDC, USDT).
**Doc vs real:** all six **refused a 170-day window** with HTTP 400, code **-6021 "Query time range too large"**, although the docs allow 6 months. Real wins: the usable span is shorter than documented and is not yet known (retry with 30 days, which is the documented default width). No real record shape was obtained.
None of the six returns an id: a record is identified by all its fields (`time` + asset + amount).

#### `bfusd-subscriptions` — GET /sapi/v1/bfusd/history/subscriptionHistory · `rwusd-subscriptions` — GET /sapi/v1/rwusd/history/subscriptionHistory
Time: `rows[].time`. Seen: refused (-6021).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].asset | string | asset paid (USDT or USDC) |
| rows[].amount | decimal string | amount paid |
| rows[].receiveAsset | string | BFUSD / RWUSD |
| rows[].receiveAmount | decimal string | amount received |
| rows[].status | string | example SUCCESS (enum not listed) |
| total | integer | row count |

#### `bfusd-redemptions` — GET /sapi/v1/bfusd/history/redemptionHistory · `rwusd-redemptions` — GET /sapi/v1/rwusd/history/redemptionHistory
Time: `rows[].time` (`arrivalTime` = arrival). Seen: refused (-6021).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].asset | string | BFUSD / RWUSD |
| rows[].amount | decimal string | amount redeemed |
| rows[].receiveAsset | string | asset received (USDT / USDC) |
| rows[].receiveAmount | decimal string | amount received |
| rows[].fee | decimal string | redemption fee |
| rows[].arrivalTime | integer | arrival, epoch ms |
| rows[].status | string | PENDING, SUCCESS |
| total | integer | row count |

#### `bfusd-rewards` — GET /sapi/v1/bfusd/history/rewardsHistory
Time: `rows[].time`. Seen: refused (-6021).

| Field | Type | Meaning |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].rewardAsset | string | reward asset (example BFUSD; the account endpoint also has `usdtProfit`) |
| rows[].rewardsAmount | decimal string | reward amount |
| rows[].BFUSDPosition | decimal string | BFUSD position the reward was computed on (capitals as documented) |
| rows[].annualPercentageRate | decimal string | APR |
| total | integer | row count |

#### `rwusd-rewards` — GET /sapi/v1/rwusd/history/rewardsHistory
Time: `rows[].time`. Seen: refused (-6021). Note: **no `rewardAsset` field** here, unlike BFUSD.

| Field | Type | Meaning |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].rewardsAmount | decimal string | reward amount |
| rows[].rwusdPosition | decimal string | RWUSD position the reward was computed on |
| rows[].annualPercentageRate | decimal string | APR |
| total | integer | row count |

---

## 7. Staking and on-chain yields histories

Shared request rule for all 13: `startTime`/`endTime` optional, "cannot be longer than 3 months"; neither sent = last 30 days; only `startTime` = the next 30 days; only `endTime` = the 30 days before it. Paging `current` (default 1), `size` (default 10, max 100). No overall lookback documented; real: 39 windows of 89 days back to July 2017 all answered 200, all empty. Docs: `investment-and-services-staking/api/rest-api/eth-staking#…`, `…/sol-staking#…`, `…/on-chain-yields#…`, `…/soft-staking#…`.
**No ETH/SOL staking history row carries an id** (`purchaseId`/`redeemId` are request filters only): a record is identified by `time` + asset + amount.

### `eth-stakes` — GET /sapi/v1/eth-staking/eth/history/stakingHistory
Weight 150 (IP) · signed. Request: `purchaseId` optional + shared rule. Id: none. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].asset | string | staked asset (ETH) |
| rows[].amount | decimal string | staked amount |
| rows[].distributeAsset | string | asset received (WBETH) |
| rows[].distributeAmount | decimal string | amount received |
| rows[].conversionRatio | decimal string | ETH per WBETH at the time |
| rows[].status | string | PENDING, SUCCESS, FAILED |
| total | integer | row count |

### `eth-redemptions` — GET /sapi/v1/eth-staking/eth/history/redemptionHistory
Weight 150 (IP) · signed. Request: `redeemId` optional + shared rule. Id: none. Time: `rows[].time` (`arrivalTime` = arrival). Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].arrivalTime | integer | arrival, epoch ms |
| rows[].asset | string | redeemed asset (WBETH) |
| rows[].amount | decimal string | redeemed amount |
| rows[].distributeAsset | string | asset received (ETH) |
| rows[].distributeAmount | decimal string | amount received |
| rows[].conversionRatio | decimal string | conversion ratio |
| rows[].status | string | PENDING, SUCCESS, FAILED |
| total | integer | row count |

### `wbeth-rewards` — GET /sapi/v1/eth-staking/eth/history/wbethRewardsHistory
Weight 150 (IP) · signed. Request: shared rule only. Id: none; one row per day (`time`). Time: `rows[].time`. Seen: empty.
These rewards accrue inside the WBETH rate; they do not post a balance change (probe sign convention: "rate rewards: not posted").

| Field | Type | Meaning |
|---|---|---|
| estRewardsInETH | decimal string | estimated rewards in ETH over the window |
| rows[].time | integer | epoch ms |
| rows[].amountInETH | decimal string | estimated rewards accrued within WBETH |
| rows[].holding | decimal string | WBETH holding balance |
| rows[].holdingInETH | decimal string | holding valued in ETH |
| rows[].annualPercentageRate | decimal string | APR |
| total | integer | row count |

### `wbeth-wraps` — GET /sapi/v1/eth-staking/wbeth/history/wrapHistory · `wbeth-unwraps` — GET /sapi/v1/eth-staking/wbeth/history/unwrapHistory
Weight 150 (IP) each · signed. Request: shared rule only. Id: none. Time: `rows[].time`. Seen: empty (both). Same fields in both; wrap is BETH → WBETH, unwrap is WBETH → BETH.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].fromAsset | string | asset given |
| rows[].fromAmount | decimal string | amount given |
| rows[].toAsset | string | asset received |
| rows[].toAmount | decimal string | amount received |
| rows[].exchangeRate | decimal string | BETH per 1 WBETH |
| rows[].status | string | PENDING, SUCCESS, FAILED |
| total | integer | row count |

### `sol-stakes` — GET /sapi/v1/sol-staking/sol/history/stakingHistory
Weight 150 (IP) · signed. Request: `purchaseId` optional + shared rule. Id: none. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].asset | string | staked asset (SOL) |
| rows[].amount | decimal string | staked amount |
| rows[].distributeAsset | string | asset received (BNSOL) |
| rows[].distributeAmount | decimal string | amount received |
| rows[].exchangeRate | decimal string | SOL per BNSOL (named `conversionRatio` on the ETH side) |
| rows[].status | string | example SUCCESS (enum not listed) |
| total | integer | row count |

### `sol-redemptions` — GET /sapi/v1/sol-staking/sol/history/redemptionHistory
Weight 150 (IP) · signed. Request: `redeemId` optional + shared rule. Id: none. Time: `rows[].time` (`arrivalTime`). Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| rows[].time | integer | epoch ms |
| rows[].arrivalTime | integer | arrival, epoch ms |
| rows[].asset | string | redeemed asset (BNSOL) |
| rows[].amount | decimal string | redeemed amount |
| rows[].distributeAsset | string | asset received (SOL) |
| rows[].distributeAmount | decimal string | amount received |
| rows[].exchangeRate | decimal string | exchange rate |
| rows[].status | string | example SUCCESS (enum not listed) |
| total | integer | row count |

### `bnsol-rewards` — GET /sapi/v1/sol-staking/sol/history/bnsolRewardsHistory
Weight 150 (IP) · signed. Request: shared rule only. Id: none; one row per day. Time: `rows[].time`. Seen: empty. Rate rewards: accrue inside the BNSOL rate, not posted to a balance.

| Field | Type | Meaning |
|---|---|---|
| estRewardsInSOL | decimal string | estimated rewards in SOL over the window |
| rows[].time | integer | epoch ms |
| rows[].amountInSOL | decimal string | reward amount |
| rows[].holding | decimal string | BNSOL holding balance |
| rows[].holdingInSOL | decimal string | holding valued in SOL |
| rows[].annualPercentageRate | decimal string | "0.5 means 50% here" |
| total | integer | row count |

### `sol-boost-rewards` — GET /sapi/v1/sol-staking/sol/history/boostRewardsHistory
Weight 150 (IP) · signed. Request: **`type` required** (CLAIM, DISTRIBUTE; default CLAIM) + shared rule. Id: none. Time: `rows[].time`. Seen: empty for both types.
The schema's example `time` is **1729520680 (10 digits, i.e. seconds)**, unlike every other history; unverified in real data.

| Field | Type | Meaning |
|---|---|---|
| rows[].time | integer | time (example in epoch seconds) |
| rows[].token | string | reward asset |
| rows[].amount | decimal string | reward amount |
| rows[].bnsolHolding | decimal string | only when type is DISTRIBUTE |
| rows[].status | string | only when type is CLAIM (example SUCCESS) |
| total | integer | row count |

### `onchain-subscriptions` — GET /sapi/v1/onchain-yields/locked/history/subscriptionRecord
Weight 50 (IP) · signed. Request: `purchaseId`, `clientId`, `asset` optional + shared rule. Id: `rows[].purchaseId`. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | string | position id |
| rows[].purchaseId | string | subscription id |
| rows[].projectId | string | project id |
| rows[].clientId | string | client-supplied id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | asset |
| rows[].amount | decimal string | subscribed amount |
| rows[].lockPeriod | string | lock period in days |
| rows[].type | string | NORMAL, AUTO |
| rows[].sourceAccount | string | SPOT, FUNDING, SPOTANDFUNDING |
| rows[].amtFromSpot | decimal string | only if SPOTANDFUNDING |
| rows[].amtFromFunding | decimal string | only if SPOTANDFUNDING |
| rows[].status | string | PURCHASING, SUCCESS, FAILED |
| total | integer | row count |

### `onchain-redemptions` — GET /sapi/v1/onchain-yields/locked/history/redemptionRecord
Weight 50 (IP) · signed. Request: `positionId`, `redeemId`, `asset` optional + shared rule. Id: `rows[].redeemId`. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].positionId | string | position id |
| rows[].redeemId | integer | redemption id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | asset |
| rows[].lockPeriod | string | lock period in days |
| rows[].amount | decimal string | redeemed amount |
| rows[].originalAmount | decimal string | original amount |
| rows[].type | string | NORMAL (maturity), EARLY, CONVERT (to Simple Earn flexible) |
| rows[].deliverDate | string | redemption date, epoch ms **as text** |
| rows[].lossAmount | decimal string | profit lost on early redemption |
| rows[].isComplete | boolean | complete |
| rows[].rewardAsset | string | reward asset |
| rows[].rewardAmt | decimal string | reward amount |
| rows[].status | string | CREATED, PAID |
| total | integer | row count |

### `onchain-rewards` — GET /sapi/v1/onchain-yields/locked/history/rewardsRecord
Weight 50 (IP) · signed. Request: `positionId`, `asset` optional + shared rule. Id: none; `positionId` + `asset` + `time`. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| rows[].positionId | string | position id |
| rows[].time | integer | epoch ms |
| rows[].asset | string | reward asset |
| rows[].lockPeriod | string | lock period in days |
| rows[].amount | decimal string | reward amount |
| total | integer | row count |

### `soft-staking-rewards` — GET /sapi/v1/soft-staking/history/rewardsRecord
Weight 50 (IP) · signed. Request: `asset` optional + shared rule. Id: none; `asset` + `rewardAsset` + `time`. Time: `rows[].time`. Seen: empty.

| Field | Type | Meaning |
|---|---|---|
| rows[].asset | string | held asset |
| rows[].rewards | decimal string | reward amount |
| rows[].rewardAsset | string | reward asset |
| rows[].avgAmount | decimal string | average holding the reward was computed on |
| rows[].time | integer | epoch ms |
| total | integer | row count |

Skipped on purpose (they do not move a balance): ETH quota (`/sapi/v1/eth-staking/eth/quota`), SOL quota (`/sapi/v1/sol-staking/sol/quota`), WBETH rate history (`/sapi/v1/eth-staking/eth/history/rateHistory`), BNSOL rate history (`/sapi/v1/sol-staking/sol/history/rateHistory`), BFUSD/RWUSD quota and rate history, Simple Earn rate history, product lists, personal left quota, subscription previews. All exist in the current docs.

---

## 8. Crypto loans

The Crypto Loan product in the current docs has one tag only, `flexible-rate` (12 paths, all `/sapi/v2/loan/flexible/…` plus `/sapi/v2/loan/interestRateHistory`). There is **no stable-rate (fixed-rate) crypto-loan endpoint and no `/sapi/v1/loan/income`** in the schema: see "Not found in the docs".
Shared request rule for the borrow, repay and LTV-adjustment histories: `loanCoin`, `collateralCoin` optional; `startTime`/`endTime`: "max interval … is 180 days", default "recent 90-day data"; paging `current` (default 1, max 1000), `limit` (default 10, max 100); "5 requests per second per account". None of the flexible-loan rows has an id: `loanCoin` + `collateralCoin` + time + amount identify a record. Docs: `investment-and-services-crypto-loan/api/rest-api/flexible-rate#…`.

### `loan-flexible-borrows` — GET /sapi/v2/loan/flexible/borrow/history
Weight 400 (IP) · signed. Id: none. Time: `rows[].borrowTime`. Seen: empty (20 windows of 179 days back to July 2017, all 200).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanCoin | string | borrowed asset |
| rows[].initialLoanAmount | decimal string | amount borrowed |
| rows[].collateralCoin | string | collateral asset |
| rows[].initialCollateralAmount | decimal string | collateral pledged |
| rows[].borrowTime | integer | epoch ms |
| rows[].status | string | SUCCESS, FAILED, PENDING |
| total | integer | row count |

### `loan-flexible-repays` — GET /sapi/v2/loan/flexible/repay/history
Weight 400 (IP) · signed. Id: none. Time: `rows[].repayTime`. Seen: empty.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanCoin | string | repaid asset |
| rows[].repayAmount | decimal string | amount repaid |
| rows[].collateralCoin | string | collateral asset |
| rows[].collateralReturn | decimal string | collateral returned |
| rows[].repayStatus | string | REPAID, REPAYING, FAILED |
| rows[].repayTime | integer | epoch ms |
| total | integer | row count |

### `loan-flexible-ltv-adjustments` (not probed) — GET /sapi/v2/loan/flexible/ltv/adjustment/history
Weight **400 (UID)** · signed. Id: none. Time: `rows[].adjustTime`. Seen: not probed.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanCoin | string | loan asset |
| rows[].collateralCoin | string | collateral asset |
| rows[].direction | string | example ADDITIONAL (the request enum of POST /sapi/v2/loan/flexible/adjust/ltv is ADDITIONAL, REDUCED) |
| rows[].collateralAmount | decimal string | collateral added or removed |
| rows[].preLTV | decimal string | LTV before |
| rows[].afterLTV | decimal string | LTV after |
| rows[].adjustTime | integer | epoch ms |
| total | integer | row count |

### `loan-flexible-liquidations` (not probed) — GET /sapi/v2/loan/flexible/liquidation/history
Weight 400 (IP) · signed. Request: `loanCoin`, `collateralCoin`, `startTime`, `endTime` optional (no window rule documented on this one); paging `current`, `limit` (default 10, max 100); "1 request per second per account". Id: none. Time: `rows[].liquidationStartingTime`. Seen: not probed.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanCoin | string | loan asset |
| rows[].liquidationDebt | decimal string | debt liquidated |
| rows[].collateralCoin | string | collateral asset |
| rows[].liquidationCollateralAmount | decimal string | collateral sold |
| rows[].returnCollateralAmount | decimal string | collateral returned |
| rows[].liquidationFee | decimal string | fee |
| rows[].liquidationStartingPrice | decimal string | starting price |
| rows[].liquidationStartingTime | integer | epoch ms |
| rows[].status | string | description "Liquidating", example "Liquidated" |
| total | integer | row count |

### VIP loans (not probed; "available for VIP users only")
Read endpoints exist. Docs: `investment-and-services-vip-loan/api/rest-api/user-information#…`. All: signed, paging `current` (default 1, max 1000), `limit` (default 10, max 100), "1 request per second per account".

#### `vip-loan-ongoing` — GET /sapi/v1/loan/vip/ongoing/orders
Weight 400 (IP). Request: `orderId`, `collateralAccountId`, `loanCoin`, `collateralCoin` optional. Id: `rows[].orderId`. Time: `rows[].loanDate`.

| Field | Type | Meaning |
|---|---|---|
| rows[].orderId | integer | order id |
| rows[].loanCoin | string | loan asset |
| rows[].totalDebt | decimal string | outstanding debt |
| rows[].loanRate | decimal string | rate (the flexible rate for flexible loans) |
| rows[].residualInterest | decimal string | interest outstanding |
| rows[].collateralAccountId | string | comma-separated account ids |
| rows[].collateralCoin | string | comma-separated assets; `<ASSET>-Earn` when a Simple Earn flexible position is pledged |
| rows[].totalCollateralValueAfterHaircut | decimal string | collateral value after haircut |
| rows[].lockedCollateralValue | decimal string | locked collateral value |
| rows[].currentLTV | decimal string | current LTV |
| rows[].expirationTime | integer | epoch ms; 0 for flexible loans |
| rows[].loanDate | string | epoch ms **as text** |
| rows[].loanTerm | string | e.g. "30days"; "open term" for flexible loans |
| total | integer | row count |

#### `vip-loan-repays` — GET /sapi/v1/loan/vip/repay/history
Weight 400 (IP). Request: `orderId`, `loanCoin` optional; `startTime`/`endTime` max 180 days, default last 90 days. Id: none unique (`orderId` + `repayTime`). Time: `rows[].repayTime`.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanCoin | string | loan asset |
| rows[].repayAmount | decimal string | total repaid |
| rows[].repayPrincipal | decimal string | principal part |
| rows[].repayInterest | decimal string | interest part |
| rows[].collateralCoin | string | comma-separated assets |
| rows[].repayStatus | string | Repaid, Repaying, Failed |
| rows[].loanDate | string | epoch ms as text |
| rows[].repayTime | string | epoch ms as text |
| rows[].orderId | string | order id as text |
| total | integer | row count |

#### `vip-loan-interest` — GET /sapi/v1/loan/vip/accruedInterest
Weight 400 (IP). Request: `orderId`, `loanCoin` optional; `startTime`/`endTime` max 90 days, default last 90 days. Id: none (`orderId` + `accrualTime`). Time: `rows[].accrualTime`.

| Field | Type | Meaning |
|---|---|---|
| rows[].loanCoin | string | loan asset |
| rows[].principalAmount | decimal string | principal |
| rows[].interestAmount | decimal string | interest accrued |
| rows[].annualInterestRate | decimal string | annual rate |
| rows[].accrualTime | integer | epoch ms |
| rows[].orderId | integer | latest order id for the renewal order |
| total | integer | row count |

#### `vip-loan-collateral-account` — GET /sapi/v1/loan/vip/collateral/account
Weight **6000** (IP). Request: `orderId`, `collateralAccountId` optional.

| Field | Type | Meaning |
|---|---|---|
| rows[].collateralAccountId | string | collateral account id |
| rows[].collateralCoin | string | comma-separated assets |
| total | integer | row count |

#### `vip-loan-requests` — GET /sapi/v1/loan/vip/request/data
Weight 400 (UID). Id: `rows[].requestId`. Time: `rows[].loanDate` (epoch ms as text).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].loanAccountId | string | loan receiving account |
| rows[].orderId | string | order id |
| rows[].requestId | string | request id |
| rows[].loanCoin | string | loan asset |
| rows[].loanAmount | decimal string | loan amount |
| rows[].collateralAccountId | string | comma-separated account ids |
| rows[].collateralCoin | string | comma-separated assets |
| rows[].loanTerm | string | loan term |
| rows[].status | string | Accruing_Interest, Overdue, Liquidating, Repaying, Repaid, Liquidated, Pending, Failed |
| rows[].loanDate | string | epoch ms as text |
| total | integer | row count |

---

## 9. Margin

Every margin history below was **refused on the probed account (-3003 "Margin account does not exist")**; only `margin-account` and `margin-isolated-account` answered. No real shape exists for this section. Docs: `core-trading-margin-trading/api/rest-api/…`.

### `margin-transfers` — GET /sapi/v1/margin/transfer
Weight 1 (IP) · signed · doc `transfer#get-cross-margin-transfer-history`
Request: all optional. `asset`, `type` (ROLL_IN, ROLL_OUT), `isolatedSymbol`. `startTime`/`endTime`: "max interval … is 30 days", default last 7 days; no overall lookback documented. Paging `current` (default 1), `size` (default 10, max 100). Descending order.
**This is also the isolated-margin transfer history**: the old `/sapi/v1/margin/isolated/transfer` is not in the current docs; isolated rows are selected with `isolatedSymbol` and carry `transFrom`/`transTo` = ISOLATED_MARGIN plus `fromSymbol`/`toSymbol`.
Id: `rows[].txId`. Time: `rows[].timestamp`. Seen: refused (-3003).
The schema's example `timestamp` is 1566898617 (10 digits); every other margin example is in ms. Unverified.

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].amount | decimal string | amount |
| rows[].asset | string | asset |
| rows[].status | string | example CONFIRMED (enum not listed here) |
| rows[].timestamp | integer | time |
| rows[].txId | integer | transfer id |
| rows[].type | string | ROLL_IN, ROLL_OUT |
| rows[].transFrom | string | SPOT, FUTURES, FIAT, DELIVERY, MINING, ISOLATED_MARGIN, FUNDING, MOTHER_SPOT, OPTION, SUB_SPOT, SUB_MARGIN, CROSS_MARGIN |
| rows[].transTo | string | same enum as `transFrom` |
| rows[].fromSymbol | string | isolated pair the funds left (isolated rows) |
| rows[].toSymbol | string | isolated pair the funds entered (isolated rows) |
| total | integer | row count |

### `margin-borrow-repay` — GET /sapi/v1/margin/borrow-repay
Weight 10 (IP) · signed · doc `borrow-repay#query-borrow-repay-records-in-margin-account`
Request: **`type` required** (BORROW, REPAY). "`txId` or `startTime` must be sent. `txId` takes precedence." `asset`, `isolatedSymbol` optional. Window: "If an asset is sent, data within 30 days before endTime; if an asset is not sent, data within 7 days before endTime"; default the recent 7 days. Paging `current` (default 1), `size` (default 10, max 100). No overall lookback documented.
Id: `rows[].txId`. Time: `rows[].timestamp`. Seen: refused (-3003).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].type | string | cross borrow: AUTO, MANUAL; cross repay: MANUAL, AUTO, BNB_AUTO_REPAY, POINT_AUTO_REPAY; isolated borrow/repay: AUTO, MANUAL |
| rows[].isolatedSymbol | string | isolated pair; not returned for cross margin |
| rows[].amount | decimal string | total amount borrowed/repaid |
| rows[].asset | string | asset |
| rows[].interest | decimal string | interest repaid |
| rows[].principal | decimal string | principal repaid |
| rows[].status | string | PENDING, CONFIRMED, FAILED |
| rows[].timestamp | integer | epoch ms |
| rows[].txId | integer | transaction id |
| total | integer | row count |

### `margin-interest` — GET /sapi/v1/margin/interestHistory
Weight 1 (IP) · signed · doc `borrow-repay#get-interest-history`
Request: all optional. `asset`, `isolatedSymbol` (omitted = cross margin data). `startTime`: "Only supports querying data from the past 90 days"; "max interval between startTime and endTime is 30 days"; default last 7 days. Paging `current` (default 1), `size` (default 10, max 100). Descending order.
Id: `rows[].txId` together with `interestAccuredTime` and `asset` (one borrow accrues hourly). Time: `rows[].interestAccuredTime` (spelling as documented). Seen: refused (-3003).

| Field | Type | Meaning / enum |
|---|---|---|
| rows[].txId | integer | transaction id |
| rows[].interestAccuredTime | integer | accrual time, epoch ms |
| rows[].asset | string | asset charged |
| rows[].rawAsset | string | original asset; not returned for isolated margin |
| rows[].principal | decimal string | principal |
| rows[].interest | decimal string | interest charged |
| rows[].interestRate | decimal string | rate |
| rows[].type | string | PERIODIC (per hour), ON_BORROW (first charge), PERIODIC_CONVERTED (per hour, converted into BNB), ON_BORROW_CONVERTED, PORTFOLIO (daily on portfolio-margin negative balance) |
| rows[].isolatedSymbol | string | isolated pair; not returned for cross margin |
| total | integer | row count |

### `margin-capital-flow` — GET /sapi/v1/margin/capital-flow
Weight 100 (IP) · signed · doc `account#query-cross-isolated-margin-capital-flow`
Request: all optional. `asset`, `symbol` ("Mandatory for Isolated data"), `type` (enum below). "Only supports querying the data of the last 90 days"; "time between startTime and endTime cannot be longer than 7 days"; default the most recent 7 days. `fromId` (returns id > fromId inside the window; "does not extend the time range"), `limit` default 500, max 1000.
Id: `[].id` (`tranId` also). Time: `[].timestamp`. Seen: refused (-3003); a window older than 90 days answered **HTTP 500, code -4047 "Only supports querying the data of the last 90 days"**.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | integer | flow id |
| [].tranId | integer | transaction id |
| [].timestamp | integer | epoch ms |
| [].asset | string | asset |
| [].symbol | string | isolated pair (isolated rows) |
| [].type | string | TRANSFER, BORROW, REPAY, BUY_INCOME, BUY_EXPENSE, SELL_INCOME, SELL_EXPENSE, TRADING_COMMISSION, BUY_LIQUIDATION, SELL_LIQUIDATION, REPAY_LIQUIDATION, OTHER_LIQUIDATION, LIQUIDATION_FEE, SMALL_BALANCE_CONVERT, COMMISSION_RETURN, SMALL_CONVERT |
| [].amount | decimal string | signed amount |
| [].note | string | INSTITUTIONAL_LOAN_TRANSFER, INSTITUTIONAL_LOAN_BORROW, INSTITUTIONAL_LOAN_REPAY |

### `margin-liquidations` — GET /sapi/v1/margin/forceLiquidationRec
Weight 1 (IP) · signed · doc `trade#get-force-liquidation-record`
Request: all optional. `startTime`, `endTime` (no window rule documented), `isolatedSymbol`; paging `current` (default 1), `size` (default 10, max 100). Descending order.
Id: `rows[].orderId`. Time: `rows[].updatedTime`. Seen: refused (-3003).

| Field | Type | Meaning |
|---|---|---|
| rows[].avgPrice | decimal string | average fill price |
| rows[].executedQty | decimal string | executed quantity |
| rows[].orderId | integer | liquidation order id |
| rows[].price | decimal string | order price |
| rows[].qty | decimal string | order quantity |
| rows[].side | string | order side (example SELL) |
| rows[].symbol | string | pair |
| rows[].timeInForce | string | example GTC |
| rows[].isIsolated | boolean | isolated margin |
| rows[].updatedTime | integer | epoch ms |
| total | integer | row count |

### `margin-small-liability` — GET /sapi/v1/margin/exchange-small-liability-history
Weight **100 (UID)** · signed · doc `trade#get-small-liability-exchange-history`
Request: **`current` and `size` required** (default 1 and 10, max 100); `startTime`, `endTime` optional (no window rule documented).
Id: none; `asset` + `targetAsset` + `timestamp`. Time: `rows[].timestamp`. Seen: refused (-3003).

| Field | Type | Meaning |
|---|---|---|
| total | integer | row count |
| rows[].asset | string | liability asset exchanged |
| rows[].amount | decimal string | liability amount |
| rows[].targetAsset | string | asset paid with |
| rows[].targetAmount | decimal string | amount paid |
| rows[].bizType | string | example EXCHANGE_SMALL_LIABILITY |
| rows[].timestamp | integer | epoch ms |

### `margin-trades` (not probed) — GET /sapi/v1/margin/myTrades
Weight 10 (IP) · signed · doc `trade#query-margin-accounts-trade-list`
Request: **`symbol` required**; `isIsolated` ("TRUE"/"FALSE" text, default FALSE), `orderId`, `fromId`. `startTime`/`endTime`: "Less than 24 hours between startTime and endTime"; without `fromId` "the trades within 24 hours are returned"; with `fromId` trades with id ≥ fromId. `limit` default 500, max 1000. So history is walked by `fromId`, not by time.
Id: `[].id` (per symbol, and per cross/isolated). Time: `[].time`. Seen: not probed.
Unlike Spot `my-trades`, there is **no `quoteQty` and no `orderListId`** documented.

| Field | Type | Meaning |
|---|---|---|
| [].commission | decimal string | commission |
| [].commissionAsset | string | commission asset |
| [].id | integer | trade id |
| [].isBestMatch | boolean | best match |
| [].isBuyer | boolean | buyer side |
| [].isMaker | boolean | maker side |
| [].orderId | integer | order id |
| [].price | decimal string | price |
| [].qty | decimal string | quantity |
| [].symbol | string | pair |
| [].isIsolated | boolean | isolated margin |
| [].time | integer | epoch ms |

### `margin-liquidation-loan` (not probed) — GET /sapi/v1/margin/liquidation-loan
Weight 100 (UID) · signed · doc `trade#query-liquidation-loan`. Request: none. The cross-margin deficit left after a bankrupt liquidation ("denominated in USDC").

| Field | Type | Meaning |
|---|---|---|
| asset | string | asset of the liquidation loan (USDC by default) |
| amount | decimal string | total liquidation loan |
| repaidAmount | decimal string | repaid so far |
| remainingAmount | decimal string | outstanding |

### `margin-liquidation-loan-repays` (not probed) — GET /sapi/v1/margin/liquidation-loan/repay-history
Weight 100 (UID) · signed · doc `trade#query-liquidation-loan-repay-history`
Request: `startTime` (default 7 days ago), `endTime` (default now); "maximum query range is 90 days; if startTime is earlier than 90 days ago, it will be clamped". Paging `current` (default 1), `size` (default 50). Only SUCCESS and PENDING rows.
Id: `rows[].repayId`. Time: `rows[].createTime`.

| Field | Type | Meaning / enum |
|---|---|---|
| total | integer | row count |
| rows[].repayId | integer | repayment id |
| rows[].asset | string | asset used |
| rows[].amount | decimal string | repayment amount |
| rows[].status | string | SUCCESS, PENDING |
| rows[].createTime | integer | epoch ms |

Margin orders (`/sapi/v1/margin/allOrders`, `order`, `openOrders`, OCO lists) are in the docs and were left out on purpose. `margin-account` and `margin-isolated-account` are in section 3.

---

## 10. Futures (USDⓈ-M on `fapi.binance.com`, COIN-M on `dapi.binance.com`)

Balances, accounts and positions (`um-balance`, `um-account`, `um-positions`, `cm-balance`, `cm-account`, `cm-positions`) are in section 3. Funding fees are rows of the income history (`incomeType` FUNDING_FEE); there is no separate account-level funding endpoint.

### `um-income` — GET /fapi/v1/income
Host fapi.binance.com · weight 30 (IP, fapi pool) · signed · doc `core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-income-history`
Request: all optional. `symbol`, `incomeType` (enum below; omitted = all kinds). `startTime`/`endTime` both inclusive; no per-query maximum documented; default "the recent 7-day data". "Income history only contains data for the last three months." `limit` default 100, max 1000; no page parameter (advance `startTime`).
Id: `tranId`, "unique in the same incomeType for a user" → key = `incomeType` + `tranId`, as the documents say (not `symbol` as well: it is empty for transfers, and an empty key part is refused). Time: `time`. Seen: empty; a window older than 3 months answered HTTP 200 with no rows (no error).
`incomeType` enum (23): TRANSFER, WELCOME_BONUS, REALIZED_PNL, FUNDING_FEE, COMMISSION, INSURANCE_CLEAR, REFERRAL_KICKBACK, COMMISSION_REBATE, API_REBATE, CONTEST_REWARD, CROSS_COLLATERAL_TRANSFER, OPTIONS_PREMIUM_FEE, OPTIONS_SETTLE_PROFIT, INTERNAL_TRANSFER, AUTO_EXCHANGE, DELIVERED_SETTELMENT (spelling as documented), COIN_SWAP_DEPOSIT, COIN_SWAP_WITHDRAW, POSITION_LIMIT_INCREASE_FEE, STRATEGY_UMFUTURES_TRANSFER, FEE_RETURN, BFUSD_REWARD, SPECIAL_FUNDING_FEE.

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | trade symbol, if existing (empty for transfers) |
| [].incomeType | string | enum above |
| [].income | decimal string | signed income amount |
| [].asset | string | income asset |
| [].info | string | extra information (free text; e.g. the transfer direction) |
| [].time | integer | epoch ms |
| [].tranId | integer | transaction id |
| [].tradeId | string | trade id, if existing (empty text otherwise) |

### `cm-income` — GET /dapi/v1/income
Host dapi.binance.com · weight 20 (IP, dapi pool) · signed · doc `core-trading-derivatives-trading-coin-m-futures/api/rest-api/account#get-income-history`
Request: all optional. `symbol`, `incomeType` (enum below). `startTime`/`endTime` inclusive: "can not be longer than 1 year"; default when omitted not documented on this page. No overall lookback documented (real: 10 windows of 364 days back to 2017 all answered 200). `limit` default 100, max 1000.
Id: `incomeType` + `tranId`. Time: `time`. Seen: empty.
`incomeType` enum (7): TRANSFER, WELCOME_BONUS, FUNDING_FEE, REALIZED_PNL, COMMISSION, INSURANCE_CLEAR, DELIVERED_SETTELMENT.

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | contract symbol |
| [].incomeType | string | enum above |
| [].income | decimal string | signed income amount |
| [].asset | string | asset |
| [].info | string | extra information, e.g. "WITHDRAW" |
| [].time | integer | epoch ms |
| [].tranId | **string** | transaction id as text (integer on USDⓈ-M) |
| [].tradeId | string | trade id |

### `um-trades` (not probed) — GET /fapi/v1/userTrades
Host fapi.binance.com · weight 5 (IP, fapi pool) · signed · doc `…usd-s-m-futures/api/rest-api/trade#account-trade-list`
Request: **`symbol` required** (no `pair` parameter). `orderId` optional. `startTime`/`endTime`: "cannot be longer than 7 days"; default last 7 days; "Only support querying trade in the past 3 months". `fromId` "cannot be sent with startTime or endTime". `limit` default 500, max 1000.
Id: `id` (per symbol). Time: `time`. Seen: not probed.

| Field | Type | Meaning |
|---|---|---|
| [].buyer | boolean | account was the buyer |
| [].commission | decimal string | commission |
| [].commissionAsset | string | commission asset |
| [].id | integer | trade id |
| [].maker | boolean | account was the maker |
| [].orderId | integer | order id |
| [].price | decimal string | price |
| [].qty | decimal string | quantity |
| [].quoteQty | decimal string | quote quantity; "0" for COIN-M symbols |
| [].baseQty | decimal string | base quantity; "0" for USDⓈ-M symbols |
| [].marginAsset | string | margin asset |
| [].realizedPnl | decimal string | realized PnL of this trade |
| [].side | string | side (example SELL) |
| [].positionSide | string | position side (example SHORT) |
| [].symbol | string | symbol |
| [].pair | string | pair |
| [].time | integer | epoch ms |

### `cm-trades` (not probed) — GET /dapi/v1/userTrades
Host dapi.binance.com · weight "20 with symbol, 40 with pair (after CM migration: 5 flat)" (IP, dapi pool) · signed · doc `…coin-m-futures/api/rest-api/trade#account-trade-list`
Request: **"Either symbol or pair must be sent"; not both**. "Pair and fromId cannot be sent together"; `orderId` only with `symbol`; with `pair`, trades of all the pair's symbols are returned. `startTime`/`endTime` ≤ 7 days, default last 7 days; "Only support querying trade in the past 3 months"; `fromId` not with times. `limit` default **50**, max 1000.
Id: `id` (per symbol; the schema's description says "positionId", it is the trade id). Time: `time`. Seen: not probed.

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | contract symbol, e.g. BTCUSD_200626 |
| [].id | integer | trade id |
| [].orderId | integer | order id |
| [].pair | string | pair, e.g. BTCUSD |
| [].side | string | trading side (example SELL) |
| [].price | decimal string | price |
| [].qty | decimal string | quantity (contracts) |
| [].realizedPnl | decimal string | realized PnL |
| [].marginAsset | string | margin asset |
| [].baseQty | decimal string | base quantity; "0" for USDⓈ-M symbols |
| [].quoteQty | decimal string | quote quantity; "0" for COIN-M symbols |
| [].commission | decimal string | fee, in crypto |
| [].commissionAsset | string | fee asset |
| [].time | integer | epoch ms |
| [].positionSide | string | position side (example BOTH) |
| [].buyer | boolean | account was the buyer |
| [].maker | boolean | account was the maker |

### `um-exchange-info` (not probed) — GET /fapi/v1/exchangeInfo
Host fapi.binance.com · weight 1 (IP, fapi pool) · unsigned · doc `core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#exchange-information` (read 2026-11-27)
Request: none. Id: `symbols[].symbol`. Time: none (`serverTime`: "please ignore"). Seen: not probed.
M7 reads it at the start of each sync for its `rateLimits` (the `REQUEST_WEIGHT` per minute that the fapi pool's limiter keeps under, plan Decision 4) and stores the symbol list; the rest is stored as reference data. A read that cannot be fetched or typed never stops a sync: the host keeps 600 a minute and the label is flagged.

| Field | Type | Meaning |
|---|---|---|
| timezone | string | "UTC" |
| serverTime | integer | epoch ms; "please ignore", use `/fapi/v1/time` |
| exchangeFilters[] | string | exchange filters (documented as strings; the example is empty) |
| rateLimits[] | array | one object per limit |
| rateLimits[].rateLimitType | string | example REQUEST_WEIGHT (enum not listed) |
| rateLimits[].interval | string | example MINUTE (enum not listed) |
| rateLimits[].intervalNum | integer | interval multiplier |
| rateLimits[].limit | integer | limit value (example 2400) |
| assets[].asset | string | asset |
| assets[].marginAvailable | boolean | usable as margin in multi-assets mode |
| assets[].autoAssetExchange | decimal string | auto-exchange threshold in multi-assets margin mode |
| symbols[].symbol | string | contract symbol |
| symbols[].pair | string | underlying pair |
| symbols[].contractType | string | e.g. PERPETUAL |
| symbols[].deliveryDate | integer | epoch ms |
| symbols[].onboardDate | integer | epoch ms |
| symbols[].status | string | e.g. TRADING |
| symbols[].maintMarginPercent, requiredMarginPercent | decimal string | "ignore" |
| symbols[].baseAsset, quoteAsset, marginAsset | string | assets |
| symbols[].pricePrecision, quantityPrecision, baseAssetPrecision, quotePrecision | integer | precisions ("do not use as tickSize/stepSize") |
| symbols[].underlyingType | string | e.g. COIN |
| symbols[].underlyingSubType[] | string | e.g. STORAGE |
| symbols[].settlePlan | integer | settlement plan |
| symbols[].triggerProtect | decimal string | threshold for algo orders with "priceProtect" |
| symbols[].filters[] | array | oneOf by `filterType` (PRICE_FILTER, LOT_SIZE, …); fields vary by kind |
| symbols[].filters[].filterType | string | filter kind |
| symbols[].orderTypes[] | string | order types |
| symbols[].timeInForce[] | string | time in force values |
| symbols[].liquidationFee | decimal string | liquidation fee rate |
| symbols[].marketTakeBound | decimal string | the most a market order may move from the mark price |

### `cm-exchange-info` (not probed) — GET /dapi/v1/exchangeInfo
Host dapi.binance.com · weight 1 (IP, dapi pool) · unsigned · doc `core-trading-derivatives-trading-coin-m-futures/api/rest-api/market-data#exchange-information` (read 2026-11-27)
Request: none. Id: `symbols[].symbol`. Time: none (`serverTime`: "Ignore please"). Seen: not probed.
Read as `um-exchange-info` is, for the dapi pool's limit. A read that cannot be fetched or typed never stops a sync: the host keeps 600 a minute and the label is flagged.

| Field | Type | Meaning |
|---|---|---|
| timezone | string | "UTC" |
| serverTime | integer | epoch ms; "Ignore please", use `/dapi/v1/time` |
| exchangeFilters[] | string | exchange filters. **Docs disagree with themselves**: the schema says `string[]`, the page's own example is `[ [] ]` (an array of empty arrays). Catalogued as the schema says; an answer that is not strings is flagged and the read skipped (the limit stays 600), so the first real answer settles the type. |
| rateLimits[] | array | one object per limit |
| rateLimits[].rateLimitType | string | example REQUEST_WEIGHT (enum not listed) |
| rateLimits[].interval | string | example MINUTE (enum not listed) |
| rateLimits[].intervalNum | integer | interval multiplier |
| rateLimits[].limit | integer | limit value |
| symbols[].symbol | string | contract symbol, e.g. BTCUSD_200925 |
| symbols[].pair | string | underlying pair |
| symbols[].contractType | string | e.g. CURRENT_QUARTER, PERPETUAL |
| symbols[].deliveryDate | integer | epoch ms |
| symbols[].onboardDate | integer | epoch ms |
| symbols[].contractStatus | string | e.g. TRADING |
| symbols[].contractSize | integer | contract size |
| symbols[].baseAsset, quoteAsset, marginAsset | string | assets |
| symbols[].pricePrecision, quantityPrecision, baseAssetPrecision, quotePrecision, equalQtyPrecision | integer | precisions (`equalQtyPrecision` "ignore") |
| symbols[].triggerProtect | decimal string | threshold for algo orders with "priceProtect" |
| symbols[].maintMarginPercent, requiredMarginPercent | decimal string | "ignore" |
| symbols[].underlyingType | string | e.g. COIN |
| symbols[].underlyingSubType[] | string | underlying sub-types. **Docs disagree with themselves**: the schema says `string[]`, the example is `[ [] ]`. Catalogued as the schema says, settled by the first real answer as `exchangeFilters` is. |
| symbols[].filters[] | array | oneOf by `filterType`; fields vary by kind |
| symbols[].filters[].filterType | string | filter kind |
| symbols[].orderTypes[] | string | order types |
| symbols[].timeInForce[] | string | time in force values |
| symbols[].liquidationFee | decimal string | liquidation fee rate |
| symbols[].marketTakeBound | decimal string | the most a market order may move from the mark price |

Also present in the docs and left out: `/fapi/v1/forceOrders`, `/dapi/v1/forceOrders` (liquidation orders), `/fapi/v1/positionMargin/history`, `/dapi/v1/positionMargin/history`, order histories, and the async download endpoints (`/fapi/v1/income/asyn`, `/fapi/v1/trade/asyn`, and the `dapi` equivalents) that return a download link for older data.

---

## 11. Options (host `eapi.binance.com`) — not probed

Docs: `core-trading-derivatives-trading-options/api/rest-api/…` (schema re-read 2026-11-27). All signed (USER_DATA) but `opt-exchange-info`. The account endpoint in the current docs is `/eapi/v1/marginAccount`; **`/eapi/v1/account` is not in the docs**.

An account with no options account: the docs give no answer for it. The options error-code page lists `-5002` USER_NOT_ACCESS ("Option user not access"); M7 takes that code from `eapi` as "not present" (asked again after 7 days), as `-3003` is for margin. Unverified. Options signatures are documented as HMAC only ("Signing per host"): an Ed25519 key refused on a signed request there (HTTP 401 or `-1022`) leaves the host unasked until the retry or a key update and is flagged, never a rejected key (design §5.1).

### `opt-account` — GET /eapi/v1/marginAccount
Host eapi.binance.com · weight 3 (IP) · signed · doc `account#option-margin-account-information`
Request: none. Id: `asset[].asset`. Time: `time`.
The Options wallet's report: each asset's `equity`. In the schema's example `equity` = `marginBalance` + `unrealizedPNL` (10099.448 − 5.00138 = 10094.44662), so `marginBalance` is the part the bill rows change; reconciliation compares the bills with its change, as futures compare with `walletBalance` (design §5.11). `adjustedEquity` is described (Chinese text) as margin balance plus the value of long BTC positions.

| Field | Type | Meaning |
|---|---|---|
| asset[].asset | string | asset (note: the array itself is named `asset`) |
| asset[].marginBalance | decimal string | margin balance |
| asset[].equity | decimal string | equity |
| asset[].available | decimal string | available |
| asset[].initialMargin | decimal string | initial margin |
| asset[].maintMargin | decimal string | maintenance margin |
| asset[].unrealizedPNL | decimal string | unrealized PnL |
| asset[].adjustedEquity | decimal string | adjusted equity |
| greek[].underlying | string | underlying, e.g. BTCUSDT |
| greek[].delta, .gamma, .theta, .vega | decimal string | account greeks per underlying (4 fields) |
| time | integer | epoch ms |
| canTrade, canDeposit, canWithdraw | boolean | capabilities |
| reduceOnly | boolean | reduce-only mode |
| tradeGroupId | integer | trade group id |

### `opt-positions` — GET /eapi/v1/position
Host eapi.binance.com · weight 5 (IP) · signed · doc `trade#option-position-information`
Request: `symbol` optional (no time range). Id: `[].symbol` (+ `side`). Time: `[].time`.

| Field | Type | Meaning |
|---|---|---|
| [].entryPrice | decimal string | average entry price |
| [].symbol | string | option symbol, e.g. BTC-200730-9000-C |
| [].side | string | position direction (example SHORT) |
| [].quantity | decimal string | positions; positive long, negative short |
| [].markValue | decimal string | current market value |
| [].unrealizedPNL | decimal string | unrealized PnL |
| [].markPrice | decimal string | mark price |
| [].strikePrice | decimal string | strike price |
| [].expiryDate | integer | exercise time, epoch ms |
| [].priceScale | integer | price decimals |
| [].quantityScale | integer | quantity decimals |
| [].optionSide | string | option type (example CALL) |
| [].quoteAsset | string | quote asset |
| [].time | integer | last update, epoch ms |
| [].bidQuantity | decimal string | open buy order quantity |
| [].askQuantity | decimal string | open sell order quantity |

### `opt-bill` — GET /eapi/v1/bill ("Account Funding Flow")
Host eapi.binance.com · weight 1 (IP) · signed · doc `account#account-funding-flow`
Request: **`currency` required** (enum USDT, "only support USDT as of now"). `recordId` (return that record and later ones; "the latest data is returned by default"), `startTime`, `endTime` optional, no per-query maximum documented; "Only support querying data in the past 3 months" (the error page: `-6073` "Search window is restricted to recent 90 days only"). `limit` default 100, max 1000.
Walked by time windows, not by `recordId`: one window over the last 90 days on the first walk, from the last fetch minus 7 days later, and a full answer (1000) has its window split in two until each half fits, so which records a full answer holds (the newest, by "the latest data is returned by default") does not matter. Whether `recordId` combines with a time range is not documented.
Id: `[].id`. Time: `[].createDate`.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | integer | record id |
| [].asset | string | asset |
| [].amount | decimal string | positive inflow, negative outflow |
| [].symbol | string | option symbol; only for contract-related types; omitted for TRANSFER; "older records may not include this field" |
| [].type | string | types named in the docs: FEE, CONTRACT, EXERCISE_PNL, EXERCISE_FEE, TRANSFER (no complete enum given; catalogued as this enum, so another value is flagged) |
| [].createDate | integer | epoch ms |

### `opt-trades` — GET /eapi/v1/userTrades
Host eapi.binance.com · weight 5 (IP) · signed · doc `trade#account-trade-list`
Request: **`symbol` required** (the option symbol). `fromId` ("Trade id to fetch from. Default gets most recent trades"), `startTime`, `endTime` optional; "Only supports querying trades in the past 3 months; a startTime/endTime outside that window returns -6073 SEARCH_WINDOW_RESTRICTED". `limit` default 100, max 1000.
Walked per symbol, for the symbols the stored `opt-bill` rows of the last 90 days name (a trade's premium and fee are bill rows of its symbol), by time windows as `opt-bill` is (one window over the last 90 days, split when full); `fromId` is not sent.
Id: `[].id` ("unique id"; `tradeId` also). Time: `[].time`.

| Field | Type | Meaning / enum |
|---|---|---|
| [].id | integer | unique id |
| [].tradeId | integer | trade id |
| [].orderId | integer | order id |
| [].symbol | string | option symbol |
| [].price | decimal string | trade price |
| [].quantity | decimal string | trade quantity |
| [].fee | decimal string | fee (negative = deduction) |
| [].realizedProfit | decimal string | realized PnL |
| [].side | string | order side (example BUY) |
| [].type | string | order type (example LIMIT) |
| [].liquidity | string | TAKER, MAKER |
| [].time | integer | epoch ms |
| [].priceScale | integer | price decimals |
| [].quantityScale | integer | quantity decimals |
| [].optionSide | string | option side (example CALL) |
| [].quoteAsset | string | quote asset |

### `opt-exercises` — GET /eapi/v1/exerciseRecord
Host eapi.binance.com · weight 5 (IP) · signed · doc `trade#user-exercise-record`
Request: `symbol`, `startTime`, `endTime` optional (no window rule and no lookback documented); `limit` default 1000, max 1000. Walked by time windows back to July 2017 (one window, split when full).
Id: `[].id` (string of digits). Time: `[].createDate`.

| Field | Type | Meaning |
|---|---|---|
| [].id | string | record id |
| [].currency | string | settlement currency |
| [].symbol | string | option symbol |
| [].exercisePrice | decimal string | exercise price |
| [].quantity | decimal string | quantity exercised |
| [].amount | decimal string | settlement amount |
| [].fee | decimal string | fee |
| [].createDate | integer | epoch ms |
| [].priceScale | integer | price decimals |
| [].quantityScale | integer | quantity decimals |
| [].optionSide | string | option side (example CALL) |
| [].positionSide | string | position side (example LONG) |
| [].quoteAsset | string | quote asset |

### `opt-exchange-info` (not probed) — GET /eapi/v1/exchangeInfo
Host eapi.binance.com · weight 1 (IP, eapi pool) · unsigned · doc `core-trading-derivatives-trading-options/api/rest-api/market-data#exchange-information` (read 2026-11-27)
Request: none. Id: `optionSymbols[].symbol`. Time: none (`serverTime`: "Current system time"; read as the futures hosts' is, volatile). Seen: not probed.
Read at the start of each sync for its `rateLimits` (the `REQUEST_WEIGHT` per minute the eapi pool's limiter keeps under, plan Decision 4) as `um-exchange-info` is; the rest is stored as reference data. A read that cannot be fetched or typed never stops a sync: the host keeps 600 a minute and the label is flagged. Unlike the futures pages, the filters are one object type with every field listed (no `oneOf`).

| Field | Type | Meaning |
|---|---|---|
| timezone | string | "UTC" |
| serverTime | integer | epoch ms |
| optionContracts[].baseAsset | string | base asset |
| optionContracts[].quoteAsset | string | quote asset |
| optionContracts[].underlying | string | underlying, e.g. BTCUSDT |
| optionContracts[].settleAsset | string | settlement asset |
| optionAssets[].name | string | asset name |
| optionSymbols[].expiryDate | integer | expiry, epoch ms |
| optionSymbols[].filters[].filterType | string | filter kind, e.g. PRICE_FILTER |
| optionSymbols[].filters[].minPrice, maxPrice, tickSize | decimal string | price filter |
| optionSymbols[].filters[].minQty, maxQty, stepSize | decimal string | quantity filter |
| optionSymbols[].symbol | string | option symbol, e.g. BTC-220815-50000-C |
| optionSymbols[].side | string | CALL or PUT (example CALL; enum not listed) |
| optionSymbols[].strikePrice | decimal string | strike price |
| optionSymbols[].underlying | string | underlying |
| optionSymbols[].unit | integer | quantity of the underlying per contract |
| optionSymbols[].liquidationFeeRate | decimal string | liquidation fee rate |
| optionSymbols[].minQty, maxQty | decimal string | order quantity bounds |
| optionSymbols[].initialMargin, maintenanceMargin, minInitialMargin, minMaintenanceMargin | decimal string | margin ratios |
| optionSymbols[].priceScale, quantityScale | integer | precisions |
| optionSymbols[].quoteAsset | string | quote asset |
| optionSymbols[].contractType | string | e.g. CRYPTO_OPTIONS |
| optionSymbols[].underlyingType | string | e.g. CRYPTO |
| optionSymbols[].nakedSell | boolean | naked selling allowed |
| optionSymbols[].status | string | e.g. TRADING |
| rateLimits[] | array | one object per limit |
| rateLimits[].rateLimitType | string | example REQUEST_WEIGHT (enum not listed) |
| rateLimits[].interval | string | example MINUTE (enum not listed) |
| rateLimits[].intervalNum | integer | interval multiplier |
| rateLimits[].limit | integer | limit value (example 2400) |

---

## 12. Portfolio Margin (host `papi.binance.com`) — brief, not probed

Account and balance read endpoints in the current docs (`advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account#…`), all signed:

| Endpoint | Weight | Body |
|---|---|---|
| GET /papi/v1/balance | 20 (IP) | array (or one object when `asset` is sent): `asset` string; decimal strings `totalWalletBalance`, `crossMarginAsset` (array form only; = free + locked), `crossMarginBorrowed`, `crossMarginFree`, `crossMarginInterest`, `crossMarginLocked`, `umWalletBalance`, `umUnrealizedPNL`, `cmWalletBalance`, `cmUnrealizedPNL`, `negativeBalance`; `updateTime` integer |
| GET /papi/v1/account | 20 (IP) | object: decimal strings `uniMMR`, `accountEquity` (USD), `actualEquity`, `accountInitialMargin`, `accountMaintMargin`, `virtualMaxWithdrawAmount`, `totalAvailableBalance`, `totalMarginOpenLoss`; `accountStatus` string (NORMAL, MARGIN_CALL, SUPPLY_MARGIN, REDUCE_ONLY, ACTIVE_LIQUIDATION, FORCE_LIQUIDATION, BANKRUPTED); `updateTime` integer |
| GET /papi/v1/um/account, GET /papi/v2/um/account | 5 (IP) | UM account detail (not catalogued) |
| GET /papi/v1/cm/account | 5 (IP) | CM account detail (not catalogued) |
| GET /papi/v1/um/accountConfig | 5 (IP) | UM account configuration (not catalogued) |
| GET /papi/v1/portfolio/negative-balance-exchange-record | 100 (IP) | negative-balance auto-exchange records (moves balances; not catalogued) |

Portfolio Margin Pro lives on `api.binance.com`: GET /sapi/v1/portfolio/balance (20 IP), GET /sapi/v1/portfolio/account (5 UID), GET /sapi/v2/portfolio/account (5 IP), GET /sapi/v1/portfolio/earn-asset-balance (1500 IP). The owner's key reports `enablePortfolioMarginTrading: false` and `isPortfolioMarginRetailEnabled: false`.

---

## 13. C2C, Pay, rebate

### `c2c` — GET /sapi/v1/c2c/orderMatch/listUserOrderHistory
Weight 1 (IP) · USER_DATA (the schema marks `x-signed: false` while labelling it USER_DATA; the probe signed it and got 200) · doc `investment-and-services-c2-c/api/rest-api/~#get-c2-ctrade-history`
Request: all optional. `tradeType` (BUY, SELL). Window **`startTimestamp`/`endTimestamp`** (names differ): "max interval … is 30 days", default the recent 30 days; "You can only view data from the past 6 months". Paging `page` (default 1), `rows` (default 100, max 100).
Id: `data[].orderNumber`. Time: `data[].createTime`. Seen: empty for BUY and SELL; a window older than 6 months answered HTTP 200 with no rows.

| Field | Type | Meaning / enum |
|---|---|---|
| code | string | "000000" = success |
| message | string | response message |
| data[].orderNumber | string | order number |
| data[].advNo | string | advertisement number |
| data[].tradeType | string | BUY, SELL |
| data[].asset | string | crypto asset |
| data[].fiat | string | fiat currency |
| data[].fiatSymbol | string | fiat currency symbol |
| data[].amount | decimal string | quantity in crypto |
| data[].totalPrice | decimal string | total order amount in fiat |
| data[].unitPrice | decimal string | unit price in fiat |
| data[].orderStatus | string | PENDING, TRADING, BUYER_PAYED, DISTRIBUTING, COMPLETED, IN_APPEAL, CANCELLED, CANCELLED_BY_SYSTEM |
| data[].createTime | integer | epoch ms |
| data[].commission | decimal string | transaction fee in crypto |
| data[].counterPartNickName | string | counterparty nickname (masked) |
| data[].payMethodName | string | payment method identifier |
| data[].additionalKycVerify | integer | 0 not required, 1 not verified, 2 verified |
| data[].takerCommissionRate | decimal string | taker commission rate |
| data[].takerCommission | decimal string | taker commission amount |
| data[].takerAmount | decimal string | taker trade amount |
| data[].advertisementRole | string | MAKER, TAKER |
| total | integer | total number of records |
| success | boolean | request success |

### `pay` — GET /sapi/v1/pay/transactions
Weight **3000 (UID)** · signed · doc `investment-and-services-pay/api/rest-api/~#get-pay-trade-history`
Request: all optional. `startTime`/`endTime`: "max interval … is 90 days", default the recent 90 days; "Support for querying orders within the last 18 months". `limit` default 100, max 100; no page parameter.
Id: `data[].transactionId`. Time: `data[].transactionTime`. Seen: empty; a window older than 18 months was refused (HTTP 400, code 403004 "The request has an invalid parameter").

| Field | Type | Meaning / enum |
|---|---|---|
| code | string | "000000" = success |
| message | string | response message |
| data[].orderType | string | PAY, PAY_REFUND, C2C, CRYPTO_BOX, CRYPTO_BOX_RF, C2C_HOLDING, C2C_HOLDING_RF, PAYOUT, REMITTANCE |
| data[].transactionId | string | transaction id |
| data[].transactionTime | integer | epoch ms |
| data[].amount | decimal string | positive income, negative expenditure (up to 8 decimals) |
| data[].currency | string | asset |
| data[].walletType | integer | 1 funding, 2 spot, 3 fiat, 4 or 6 card payment, 5 earn |
| data[].walletTypes[] | integer | all wallet types used (combined payments) |
| data[].fundsDetail[].currency | string | asset used |
| data[].fundsDetail[].amount | decimal string | amount used |
| data[].fundsDetail[].walletAssetCost | object | map: wallet type id ("1", "2", …) → cost amount |
| data[].fundsDetail[].walletAssetCost.<walletType> | decimal string | cost from that wallet |
| data[].payerInfo.name | string | nickname or merchant name |
| data[].payerInfo.type | string | USER, MERCHANT |
| data[].payerInfo.binanceId | string | Binance UID |
| data[].receiverInfo.name | string | nickname or merchant name |
| data[].receiverInfo.type | string | USER, MERCHANT |
| data[].receiverInfo.email | string | email |
| data[].receiverInfo.binanceId | string | Binance UID |
| data[].receiverInfo.accountId | string | Binance Pay ID |
| data[].receiverInfo.countryCode | string | international area code |
| data[].receiverInfo.phoneNumber | string | phone number |
| data[].receiverInfo.mobileCode | string | country code |
| data[].receiverInfo.extend.institutionName | string | bank or digital wallet name |
| data[].receiverInfo.extend.cardNumber | string | card number |
| data[].receiverInfo.extend.digitalWalletId | string | digital wallet id |
| success | boolean | request success |

Which `payerInfo`/`receiverInfo` fields are filled depends on `orderType` and on whether the account is sender or receiver (the doc page lists 12 cases); treat every one as nullable.

### `rebate` — GET /sapi/v1/rebate/taxQuery
Weight **12000 (UID)** · signed · doc `investment-and-services-rebate/api/rest-api/~#get-spot-rebate-history-records`
Request: all optional. `startTime`/`endTime`: "max interval … is 30 days", default the recent 7 days; "The earliest supported startTime is June 10, 2020". `page` (default 1); "Return up to 200 records per request" (no size parameter).
Id: **none**; a record is `asset` + `type` + `amount` + `updateTime`. Time: `data.data[].updateTime`. Seen: empty (79 windows back to June 2020).

| Field | Type | Meaning / enum |
|---|---|---|
| status | string | "OK" |
| type | string | "GENERAL" |
| code | string | "000000000" (nine zeros here) |
| data.page | integer | current page |
| data.totalRecords | integer | total records |
| data.totalPageNum | integer | total pages |
| data.data[].asset | string | rebate asset |
| data.data[].type | integer | 1 commission rebate, 2 referral kickback |
| data.data[].amount | decimal string | rebate amount |
| data.data[].updateTime | integer | epoch ms |

---

## 14. Spot trades

### `my-trades` — GET /api/v3/myTrades
Weight 20 without `orderId`, 5 with `orderId` (IP, `/api` pool) · signed · doc `core-trading-spot-trading/api/rest-api/account#my-trades`
Request: **`symbol` required** (one pair per request). `orderId`, `fromId`, `startTime`, `endTime` optional: "The time between startTime and endTime can't be longer than 24 hours"; "If fromId is set, it will get trades >= that fromId. Otherwise most recent trades are returned." Supported combinations: symbol; +orderId; +startTime; +endTime; +fromId; +startTime+endTime; +orderId+fromId. `limit` default 500, max 1000. No lookback limit documented; real: reached the first trade (about 13 months). History is walked with `fromId` (start at 0), not by time.
Id: `symbol` + `id` (trade ids are per symbol). Time: `time`. Seen: yes — the 13 documented fields exactly. `orderListId` was -1 on every row; `commissionAsset` was sometimes a third asset and sometimes the base asset.
A pair delisted from `exchange-info` cannot be asked for, so its trades stay invisible.

| Field | Type | Meaning |
|---|---|---|
| [].symbol | string | pair |
| [].id | integer | trade id |
| [].orderId | integer | order id |
| [].orderListId | integer | order-list id, -1 when none |
| [].price | decimal string | price |
| [].qty | decimal string | base quantity |
| [].quoteQty | decimal string | quote quantity |
| [].commission | decimal string | commission |
| [].commissionAsset | string | commission asset |
| [].time | integer | epoch ms |
| [].isBuyer | boolean | account was the buyer |
| [].isMaker | boolean | account was the maker |
| [].isBestMatch | boolean | best match |

---

## 15. Gift card, NFT, mining, sub-accounts: one line each

**Gift card** (`investment-and-services-gift-card`, 6 paths). The read endpoints are lookups; **none lists gift-card history or moves a balance** (balances move through the POST create/redeem/buy endpoints, which are writes):
- GET /sapi/v1/giftcard/verify (1 IP) — is a card number valid: `data.valid` boolean, `data.token` string, `data.amount` decimal string.
- GET /sapi/v1/giftcard/cryptography/rsa-public-key (1 IP) — RSA public key used to encrypt a card code: `data` string.
- GET /sapi/v1/giftcard/buyCode/token-limit (1 IP) — tokens and limits for stablecoin-denominated cards: `data[].coin`, `data[].fromMin`, `data[].fromMax`.
Gift-card assets sit in the Funding wallet (`funding-asset`: "Binance Pay, Binance Card, Binance Gift Card, Stock Token").

**NFT**: no NFT product and no `/sapi/v1/nft/…` path anywhere in the current docs. See "Not found in the docs".

**Mining** (`investment-and-services-mining`, 13 paths; not probed). Read endpoints that record money paid to the account (all weight 1 IP, signed; amounts are typed **number**, not string):
- GET /sapi/v1/mining/payment/list — "Earnings List": `data.accountProfits[]` with `time`, `type`, `hashTransfer`, `transferAmount`, `dayHashRate`, `profitAmount`, `coinName`, `status`.
- GET /sapi/v1/mining/payment/other — "Extra Bonus List": `data.otherProfits[]` with `time`, `coinName`, `type`, `profitAmount`, `status`.
- GET /sapi/v1/mining/payment/uid — "Mining Account Earning": `data.accountProfits[]` with `time`, `coinName`, `type`, `puid`, `subName`, `amount`.
- GET /sapi/v1/mining/hash-transfer/profit/details — "Hashrate Resale Detail": `data.profitTransferDetails[]` with `poolUsername`, `toPoolUsername`, `algoName`, `hashRate`, `day`, `amount`, `coinName`.
The other mining reads (account list, statistics, worker lists, resale configuration list, algorithm and coin lists) do not move a balance.

**Sub-accounts** (`vip-and-institutional-sub-account`, 45 paths): skipped — the owner has a single account.

**Not needed, present in the docs** (per the brief): `/sapi/v1/asset/tradeFee`, `/sapi/v1/bnbBurn` (GET status, POST toggle), `/sapi/v1/convert/orderStatus`, `/sapi/v1/simple-earn/locked/setAutoSubscribe` (write), margin orders.

---

## Not found in the docs

Searched all 24 product schemas on 2026-11-23:

| Asked for | Result |
|---|---|
| Isolated-margin transfer history (`GET /sapi/v1/margin/isolated/transfer`) | Not in the schema. The Margin "Change Log" says "GET /sapi/v1/margin/isolated/transfer will be removed, please replace it with GET /sapi/v1/margin/transfer to get total margin transfer history". Use `margin-transfers` with `isolatedSymbol`, or `universal-transfer` with the three isolated types. |
| Options account as `/eapi/v1/account` | Not in the schema. The documented account endpoint is `/eapi/v1/marginAccount` ("Option Margin Account Information"). |
| Stable-rate (fixed-rate) crypto loans: ongoing orders, borrow, repay, LTV adjustment history (`/sapi/v1/loan/ongoing/orders`, `/sapi/v1/loan/borrow/history`, `/sapi/v1/loan/repay/history`, `/sapi/v1/loan/ltv/adjustment/history`) | None in the schema. The Crypto Loan product has the `flexible-rate` tag only. (`/sapi/v1/loan/vip/fixed/…` exists but is VIP-loan market data and a write.) |
| Crypto-loan income history (`/sapi/v1/loan/income`) | Not in the schema. |
| NFT (`/sapi/v1/nft/…`) | No product, no path. |
| Auto-Invest (`/sapi/v1/lending/auto-invest/…`) | No product, no path (as the notes file already says). |
| Legacy staking (`/sapi/v1/staking/…`), futures transfer history (`/sapi/v1/futures/transfer`), BUSD convert history (`/sapi/v1/asset/convert-transfer/queryByPage`) | Not in the schema (not asked for; checked while searching). |

Found under a different shape than the brief assumed: locked-position "reward fields" are on `earn-locked-position` itself (`rewardAsset`, `rewardAmt`, `extraRewardAsset`, `extraRewardAPR`, `estExtraRewardAmt`, `boostRewardAsset`, `boostApr`, `totalBoostRewardAmt`, `nextPay`, `nextPayDate`); discount buy is the `accumulator` path family.

---

## Read endpoints that are POST

| Endpoint | Doc page (anchor) | How the page defines it |
|---|---|---|
| POST /sapi/v1/asset/get-funding-asset | Wallet → Asset → "Funding Wallet (USER_DATA)" (`core-trading-wallet/api/rest-api/asset#funding-wallet`) | description "Query Funding Wallet"; response titled "Funding Wallet"; parameters `asset`, `needBtcValuation` only |
| POST /sapi/v3/asset/getUserAsset | Wallet → Asset → "User Asset (USER_DATA)" (`core-trading-wallet/api/rest-api/asset#user-asset`) | description "Get user assets, just for positive data."; parameters `asset`, `needBtcValuation` only |

Both are in the probe and both answered 200 with a read-only key. No other catalogued read endpoint is a POST. Two more POST queries exist in the Wallet docs and are not needed: POST /sapi/v1/asset/dust-btc ("Get Assets That Can Be Converted Into BNB") and POST /sapi/v1/asset/dust-convert/query-convertible-assets ("Dust Convertible Assets").

---

## Signing per host

All hosts: API key in the `X-MBX-APIKEY` header; signed requests add `timestamp` and `signature`; `recvWindow` defaults to 5000 and cannot exceed 60000.

| Host | Doc page (title, path) | What it says about key types |
|---|---|---|
| `api.binance.com` — `/api` (Spot) | "General REST API Information" → "Request Security" (`products/spot/rest-api#request-security`) | "We support HMAC, RSA, and Ed25519 keys." Sub-sections "HMAC Keys", "RSA Keys", "Ed25519 Keys". HMAC-SHA-256 output as hex, "not case-sensitive"; RSA is RSASSA-PKCS1-v1_5 with SHA-256, base64, "Only PKCS#8 keys are supported", case-sensitive; Ed25519 base64, case-sensitive: "It is highly recommended to use Ed25519 API keys as it should provide the best performance and security out of all supported key types." `timestamp` may be milliseconds or microseconds. |
| same | "API Key Types" (`products/spot/faqs/api_key_types`) | "Ed25519 (recommended), HMAC, RSA"; "HMAC keys are deprecated. We recommend to migrate to asymmetric API keys, such as Ed25519 or RSA." |
| `api.binance.com` — `/sapi` (Wallet, Earn, Staking, Loans, Convert, Fiat, C2C, Pay, Rebate) | Wallet "General Info" → "Request Security" (`products/wallet/general-info#request-security`) | Same section as Spot: "HMAC Keys", "RSA Keys", "Ed25519 Keys", same case-sensitivity rules. |
| `api.binance.com` — `/sapi/v1/margin` | Margin "General Info" → "SIGNED (TRADE, USER_DATA, AND MARGIN) Endpoint security" (`products/margin-trading/general-info`) | "Endpoints use HMAC SHA256 signatures." plus "SIGNED Endpoint Example for POST /api/v3/order - RSA Keys" ("We support PKCS#8 currently"). Ed25519 is not mentioned on this page (same host as Wallet). |
| `fapi.binance.com` | USDⓈ-M Futures "General Info" → "Endpoint Security Type" / "SIGNED (TRADE and USER_DATA) Endpoint Security" (`products/derivatives-trading-usds-futures/general-info#endpoint-security-type`) | "Endpoints use HMAC SHA256 signatures." and "SIGNED Endpoint Examples for POST /fapi/v1/order - RSA Keys". **Ed25519 is not mentioned** on the page or in its Change Log. |
| `dapi.binance.com` | COIN-M Futures "General Info" → "Endpoint Security Type" (`products/derivatives-trading-coin-futures/general-info#endpoint-security-type`) | Same as USDⓈ-M: HMAC SHA256 and "…POST /dapi/v1/order - RSA Keys". **Ed25519 not mentioned.** |
| `eapi.binance.com` | Options "General Info" → "SIGNED (TRADE and USER_DATA) Endpoint Security" (`products/derivatives-trading-options/general-info#endpoint-security-type`) | "Endpoints use HMAC SHA256 signatures." only. **Neither RSA nor Ed25519 is mentioned.** |
| `papi.binance.com` | Portfolio Margin "General Info" → "SIGNED (TRADE and USER_DATA) Endpoint Security" (`products/derivatives-trading-portfolio-margin/general-info#endpoint-security-type`) | HMAC SHA256 and "RSA Keys - SIGNED Endpoint Examples for POST /papi/v1/um/order". **Ed25519 not mentioned.** |

Percent-encoding: the Spot "CHANGELOG" (2025-12-17) and the Wallet and Margin "Change Log" (2025-12-26) carry the same notice: from "approximately 2026-01-15 07:00 UTC … percent-encode payloads before computing signatures. Requests that do not follow this order will be rejected with -1022 INVALID_SIGNATURE." The futures, options and portfolio-margin change logs do not carry that notice (they only say, 2025-10-09, that a symbol with Chinese characters must be URL-encoded).

Conclusion: **HMAC is the only key type documented on all five hosts** (and the docs call it deprecated on the Spot side); RSA is documented on `api`, `fapi`, `dapi` and `papi` but not `eapi`; **Ed25519 is documented for `api.binance.com` only**. Whether an Ed25519 key signs `fapi`/`dapi`/`eapi`/`papi` requests is not stated in the docs and was not tested: the owner's probe used an HMAC key, which worked on `api`, `fapi` and `dapi`. Until tested, plan for Ed25519 on `api.binance.com` and keep the signer pluggable for the futures hosts.

Rate-limit pools per host (same pages, "LIMITS"): `/api` shares 6,000 weight/min per IP (header `X-MBX-USED-WEIGHT-1M`); `/sapi` has 12,000/min per endpoint for IP-rule endpoints and 180,000/min per endpoint for UID-rule endpoints (headers `X-SAPI-USED-IP-WEIGHT-1M`, `X-SAPI-USED-UID-WEIGHT-1M`), and "the IP ban escalation (HTTP 418) … does not apply to /sapi/* endpoints. Exceeding the rate limit on /sapi/* will always return HTTP 429"; `fapi`, `dapi` and `eapi` publish their limits only in their own `exchangeInfo` `rateLimits` array and report usage in `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)`; 429 then 418 (IP ban, 2 minutes to 3 days) apply there and on `/api`.

---

## Timestamps

Default everywhere: integer epoch **milliseconds**, UTC. Spot REST can return microseconds only when the request sends the header `X-MBX-TIME-UNIT: MICROSECOND` (do not send it). Exceptions, i.e. times that are text or not milliseconds:

| Endpoint | Field(s) | Documented format |
|---|---|---|
| `withdrawals` (/sapi/v1/capital/withdraw/history) | `applyTime`, `completeTime` | string "YYYY-MM-DD HH:MM:SS", described as "UTC time" (example "2019-10-12 11:12:02"); no zone suffix. Real: exactly that shape. `completeTime` "only if status = 6". |
| `travel-withdrawals` (/sapi/v1/localentity/withdraw/history) | `applyTime`, `completeTime` | same UTC text |
| `onchain-locked-position` | `purchaseTime`, `nextPayDate`, `rewardsPayDate`, `rewardsEndDate`, `deliverDate`, `nextSubscriptionDate` | string holding epoch ms digits (example "1646182276000") |
| `onchain-redemptions` | `deliverDate` | string holding epoch ms digits (its `time` is an integer) |
| `vip-loan-ongoing` | `loanDate` | string holding epoch ms digits (`expirationTime` is an integer) |
| `vip-loan-repays` | `loanDate`, `repayTime` | strings holding epoch ms digits |
| `vip-loan-requests` | `loanDate` | string holding epoch ms digits |
| `sol-boost-rewards` | `time` | integer, but the schema example is 1729520680 (10 digits = seconds). Unverified. |
| `margin-transfers` | `timestamp` | integer; the schema example is 1566898617 (10 digits) while the other margin examples are in ms. Unverified. |
| `earn-locked-position`, locked histories | `duration`, `accrualDays`, `payPeriod`, `redeemPeriod`, `lockPeriod` | not times but day counts as digit strings |
| `deposits` | `confirmTimes` | not a time: "n/m" confirmations text |

Request-side naming differences: `fiat-orders` and `fiat-payments` use `beginTime`/`endTime`; `c2c` uses `startTimestamp`/`endTimestamp`; everything else uses `startTime`/`endTime`. `cloud-mining` documents `startTime` inclusive and `endTime` exclusive; `um-income` and `cm-income` document both ends inclusive.

Daily snapshots (`snapshot`): real `updateTime` is 23:59:59 UTC of each day (the schema example 1576281599000 is also 23:59:59 UTC).

---

## Doc vs real: all differences found

| Endpoint | Difference | Take |
|---|---|---|
| `earn-flexible-redemptions`, `earn-flexible-rewards` | docs: `projectId`; real: `productId` | real name |
| `earn-flexible-position` | docs list `yesterdayAirdropPercentageRate`, `airDropAsset`: absent in real; tier rate documented number, real decimal string | nullable columns; text type |
| `convert` | real has `orderType`, `side`, `walletType`, not in docs | add three columns |
| `deposits` | real has `sourceAddress` (with `includeSource=true`), not in the response schema | add column |
| `margin-account` | real has `transferEnabled`; docs' `collateralMarginLevel`, `TotalCollateralValueInUSDT` absent in real | add column; keep the two nullable |
| `cm-account` | real `positions[].isolatedWallet`, not in docs | add column |
| `cm-positions` | real `isolatedWallet`, `notionalValue`, not in docs | add columns |
| BFUSD/RWUSD histories (6) | docs: window ≤ 6 months; real: 170 days refused, -6021 "Query time range too large" | use a shorter window (30 days untested but is the documented default) |
| `cloud-mining` | in the docs; real HTTP 404 "No message available" | unavailable for this account |
| `travel-deposits`, `travel-withdrawals` | in the docs; real -30105 "not supported for your account" | skip for this account |
| `wallet-balance` | `walletName` values are not documented; 10 real values recorded above | store the text |
| `withdrawals` | docs: `info` = "reason for withdrawal failure"; real: set on both completed rows | plain text column |
| `um-income` | docs: "only … the last three months"; real: an older window returns 200 with no rows, not an error | an empty answer does not prove absence |
| `c2c` | same: older than 6 months returns 200 and nothing | same |
| `c2c` | schema says `x-signed: false` with security type USER_DATA | sign it (the probe did) |
| Fields the docs make conditional and real did not show | `withdrawals.withdrawOrderId`, `earn-flexible-subscriptions.amtFromSpot`/`amtFromFunding` | nullable |

Everything else that returned records matched the docs field for field: `api-restrictions`, `account-info`, `account-status`, `api-trading-status`, `spot-account`, `user-asset`, `earn-account`, `bfusd-account`, `rwusd-account`, `um-balance`, `um-account` (assets and totals), `cm-balance`, `eth-staking-account`, `sol-staking-account`, `onchain-account`, `soft-staking-list` rows, `dci-accounts`, `discount-accounts`, `snapshot` (SPOT), `withdrawals`, `dividends`, `earn-flexible-subscriptions`, `my-trades`.
No real shape exists (empty or refused) for: funding wallet, locked earn, all staking/on-chain histories, loans, every margin history, futures income and positions (USDⓈ-M), fiat, C2C, Pay, rebate, dust, universal transfers, dual investment and discount buy positions. Their columns rest on the docs alone.

---

## Index of labels

One row per endpoint M7 reads, for the test that keeps the code's catalogue equal to this document. "Fields" says where the label's field table is: under its own heading, or in section 12's table for the two Portfolio Margin reads, whose fields are listed in prose there.

| Label | Method | Host | Path | Fields |
|---|---|---|---|---|
| `account-info` | GET | api.binance.com | /sapi/v1/account/info | own heading |
| `account-status` | GET | api.binance.com | /sapi/v1/account/status | own heading |
| `api-restrictions` | GET | api.binance.com | /sapi/v1/account/apiRestrictions | own heading |
| `api-trading-status` | GET | api.binance.com | /sapi/v1/account/apiTradingStatus | own heading |
| `asset-detail` | GET | api.binance.com | /sapi/v1/asset/assetDetail | own heading |
| `bfusd-account` | GET | api.binance.com | /sapi/v1/bfusd/account | own heading |
| `bfusd-redemptions` | GET | api.binance.com | /sapi/v1/bfusd/history/redemptionHistory | own heading |
| `bfusd-rewards` | GET | api.binance.com | /sapi/v1/bfusd/history/rewardsHistory | own heading |
| `bfusd-subscriptions` | GET | api.binance.com | /sapi/v1/bfusd/history/subscriptionHistory | own heading |
| `bnsol-rewards` | GET | api.binance.com | /sapi/v1/sol-staking/sol/history/bnsolRewardsHistory | own heading |
| `c2c` | GET | api.binance.com | /sapi/v1/c2c/orderMatch/listUserOrderHistory | own heading |
| `cloud-mining` | GET | api.binance.com | /sapi/v1/asset/ledger-transfer/cloud-mining/queryByPage | own heading |
| `cm-account` | GET | dapi.binance.com | /dapi/v1/account | own heading |
| `cm-balance` | GET | dapi.binance.com | /dapi/v1/balance | own heading |
| `cm-exchange-info` | GET | dapi.binance.com | /dapi/v1/exchangeInfo | own heading |
| `cm-income` | GET | dapi.binance.com | /dapi/v1/income | own heading |
| `cm-positions` | GET | dapi.binance.com | /dapi/v1/positionRisk | own heading |
| `cm-trades` | GET | dapi.binance.com | /dapi/v1/userTrades | own heading |
| `coin-config` | GET | api.binance.com | /sapi/v1/capital/config/getall | own heading |
| `convert` | GET | api.binance.com | /sapi/v1/convert/tradeFlow | own heading |
| `dci-accounts` | GET | api.binance.com | /sapi/v1/dci/product/accounts | own heading |
| `dci-positions` | GET | api.binance.com | /sapi/v1/dci/product/positions | own heading |
| `deposits` | GET | api.binance.com | /sapi/v1/capital/deposit/hisrec | own heading |
| `discount-accounts` | GET | api.binance.com | /sapi/v1/accumulator/product/sum-holding | own heading |
| `discount-positions` | GET | api.binance.com | /sapi/v1/accumulator/product/position/list | own heading |
| `dividends` | GET | api.binance.com | /sapi/v1/asset/assetDividend | own heading |
| `dust` | GET | api.binance.com | /sapi/v1/asset/dribblet | own heading |
| `earn-account` | GET | api.binance.com | /sapi/v1/simple-earn/account | own heading |
| `earn-flexible-collateral` | GET | api.binance.com | /sapi/v1/simple-earn/flexible/history/collateralRecord | own heading |
| `earn-flexible-position` | GET | api.binance.com | /sapi/v1/simple-earn/flexible/position | own heading |
| `earn-flexible-redemptions` | GET | api.binance.com | /sapi/v1/simple-earn/flexible/history/redemptionRecord | own heading |
| `earn-flexible-rewards` | GET | api.binance.com | /sapi/v1/simple-earn/flexible/history/rewardsRecord | own heading |
| `earn-flexible-subscriptions` | GET | api.binance.com | /sapi/v1/simple-earn/flexible/history/subscriptionRecord | own heading |
| `earn-locked-position` | GET | api.binance.com | /sapi/v1/simple-earn/locked/position | own heading |
| `earn-locked-redemptions` | GET | api.binance.com | /sapi/v1/simple-earn/locked/history/redemptionRecord | own heading |
| `earn-locked-rewards` | GET | api.binance.com | /sapi/v1/simple-earn/locked/history/rewardsRecord | own heading |
| `earn-locked-subscriptions` | GET | api.binance.com | /sapi/v1/simple-earn/locked/history/subscriptionRecord | own heading |
| `eth-redemptions` | GET | api.binance.com | /sapi/v1/eth-staking/eth/history/redemptionHistory | own heading |
| `eth-stakes` | GET | api.binance.com | /sapi/v1/eth-staking/eth/history/stakingHistory | own heading |
| `eth-staking-account` | GET | api.binance.com | /sapi/v2/eth-staking/account | own heading |
| `exchange-info` | GET | api.binance.com | /api/v3/exchangeInfo | own heading |
| `fiat-orders` | GET | api.binance.com | /sapi/v1/fiat/orders | own heading |
| `fiat-payments` | GET | api.binance.com | /sapi/v1/fiat/payments | own heading |
| `funding-asset` | POST | api.binance.com | /sapi/v1/asset/get-funding-asset | own heading |
| `klines` | GET | api.binance.com | /api/v3/klines | own heading |
| `loan-flexible-borrows` | GET | api.binance.com | /sapi/v2/loan/flexible/borrow/history | own heading |
| `loan-flexible-liquidations` | GET | api.binance.com | /sapi/v2/loan/flexible/liquidation/history | own heading |
| `loan-flexible-ltv-adjustments` | GET | api.binance.com | /sapi/v2/loan/flexible/ltv/adjustment/history | own heading |
| `loan-flexible-ongoing` | GET | api.binance.com | /sapi/v2/loan/flexible/ongoing/orders | own heading |
| `loan-flexible-repays` | GET | api.binance.com | /sapi/v2/loan/flexible/repay/history | own heading |
| `margin-account` | GET | api.binance.com | /sapi/v1/margin/account | own heading |
| `margin-borrow-repay` | GET | api.binance.com | /sapi/v1/margin/borrow-repay | own heading |
| `margin-capital-flow` | GET | api.binance.com | /sapi/v1/margin/capital-flow | own heading |
| `margin-interest` | GET | api.binance.com | /sapi/v1/margin/interestHistory | own heading |
| `margin-isolated-account` | GET | api.binance.com | /sapi/v1/margin/isolated/account | own heading |
| `margin-liquidation-loan` | GET | api.binance.com | /sapi/v1/margin/liquidation-loan | own heading |
| `margin-liquidation-loan-repays` | GET | api.binance.com | /sapi/v1/margin/liquidation-loan/repay-history | own heading |
| `margin-liquidations` | GET | api.binance.com | /sapi/v1/margin/forceLiquidationRec | own heading |
| `margin-small-liability` | GET | api.binance.com | /sapi/v1/margin/exchange-small-liability-history | own heading |
| `margin-trades` | GET | api.binance.com | /sapi/v1/margin/myTrades | own heading |
| `margin-transfers` | GET | api.binance.com | /sapi/v1/margin/transfer | own heading |
| `my-trades` | GET | api.binance.com | /api/v3/myTrades | own heading |
| `onchain-account` | GET | api.binance.com | /sapi/v1/onchain-yields/account | own heading |
| `onchain-locked-position` | GET | api.binance.com | /sapi/v1/onchain-yields/locked/position | own heading |
| `onchain-redemptions` | GET | api.binance.com | /sapi/v1/onchain-yields/locked/history/redemptionRecord | own heading |
| `onchain-rewards` | GET | api.binance.com | /sapi/v1/onchain-yields/locked/history/rewardsRecord | own heading |
| `onchain-subscriptions` | GET | api.binance.com | /sapi/v1/onchain-yields/locked/history/subscriptionRecord | own heading |
| `opt-account` | GET | eapi.binance.com | /eapi/v1/marginAccount | own heading |
| `opt-bill` | GET | eapi.binance.com | /eapi/v1/bill | own heading |
| `opt-exchange-info` | GET | eapi.binance.com | /eapi/v1/exchangeInfo | own heading |
| `opt-exercises` | GET | eapi.binance.com | /eapi/v1/exerciseRecord | own heading |
| `opt-positions` | GET | eapi.binance.com | /eapi/v1/position | own heading |
| `opt-trades` | GET | eapi.binance.com | /eapi/v1/userTrades | own heading |
| `pay` | GET | api.binance.com | /sapi/v1/pay/transactions | own heading |
| `pm-account` | GET | papi.binance.com | /papi/v1/account | §12 table |
| `pm-balance` | GET | papi.binance.com | /papi/v1/balance | §12 table |
| `rebate` | GET | api.binance.com | /sapi/v1/rebate/taxQuery | own heading |
| `rwusd-account` | GET | api.binance.com | /sapi/v1/rwusd/account | own heading |
| `rwusd-redemptions` | GET | api.binance.com | /sapi/v1/rwusd/history/redemptionHistory | own heading |
| `rwusd-rewards` | GET | api.binance.com | /sapi/v1/rwusd/history/rewardsHistory | own heading |
| `rwusd-subscriptions` | GET | api.binance.com | /sapi/v1/rwusd/history/subscriptionHistory | own heading |
| `snapshot` | GET | api.binance.com | /sapi/v1/accountSnapshot | own heading |
| `soft-staking-list` | GET | api.binance.com | /sapi/v1/soft-staking/list | own heading |
| `soft-staking-rewards` | GET | api.binance.com | /sapi/v1/soft-staking/history/rewardsRecord | own heading |
| `sol-boost-rewards` | GET | api.binance.com | /sapi/v1/sol-staking/sol/history/boostRewardsHistory | own heading |
| `sol-redemptions` | GET | api.binance.com | /sapi/v1/sol-staking/sol/history/redemptionHistory | own heading |
| `sol-stakes` | GET | api.binance.com | /sapi/v1/sol-staking/sol/history/stakingHistory | own heading |
| `sol-staking-account` | GET | api.binance.com | /sapi/v1/sol-staking/account | own heading |
| `sol-unclaimed-rewards` | GET | api.binance.com | /sapi/v1/sol-staking/sol/history/unclaimedRewards | own heading |
| `spot-account` | GET | api.binance.com | /api/v3/account | own heading |
| `time` | GET | api.binance.com | /api/v3/time | own heading |
| `travel-deposits` | GET | api.binance.com | /sapi/v1/localentity/deposit/history | own heading |
| `travel-withdrawals` | GET | api.binance.com | /sapi/v1/localentity/withdraw/history | own heading |
| `um-account` | GET | fapi.binance.com | /fapi/v3/account | own heading |
| `um-balance` | GET | fapi.binance.com | /fapi/v3/balance | own heading |
| `um-exchange-info` | GET | fapi.binance.com | /fapi/v1/exchangeInfo | own heading |
| `um-income` | GET | fapi.binance.com | /fapi/v1/income | own heading |
| `um-positions` | GET | fapi.binance.com | /fapi/v3/positionRisk | own heading |
| `um-trades` | GET | fapi.binance.com | /fapi/v1/userTrades | own heading |
| `universal-transfer` | GET | api.binance.com | /sapi/v1/asset/transfer | own heading |
| `user-asset` | POST | api.binance.com | /sapi/v3/asset/getUserAsset | own heading |
| `vip-loan-collateral-account` | GET | api.binance.com | /sapi/v1/loan/vip/collateral/account | own heading |
| `vip-loan-interest` | GET | api.binance.com | /sapi/v1/loan/vip/accruedInterest | own heading |
| `vip-loan-ongoing` | GET | api.binance.com | /sapi/v1/loan/vip/ongoing/orders | own heading |
| `vip-loan-repays` | GET | api.binance.com | /sapi/v1/loan/vip/repay/history | own heading |
| `vip-loan-requests` | GET | api.binance.com | /sapi/v1/loan/vip/request/data | own heading |
| `wallet-balance` | GET | api.binance.com | /sapi/v1/asset/wallet/balance | own heading |
| `wbeth-rewards` | GET | api.binance.com | /sapi/v1/eth-staking/eth/history/wbethRewardsHistory | own heading |
| `wbeth-unwraps` | GET | api.binance.com | /sapi/v1/eth-staking/wbeth/history/unwrapHistory | own heading |
| `wbeth-wraps` | GET | api.binance.com | /sapi/v1/eth-staking/wbeth/history/wrapHistory | own heading |
| `withdrawals` | GET | api.binance.com | /sapi/v1/capital/withdraw/history | own heading |

---

## Totals

- Full field tables: **110 endpoints** (111 with `klines`) = the probe's 85 (all found in the current docs at the method, path and weight the probe uses) + 22 added (`coin-config`, `asset-detail`, `earn-flexible-collateral`, `loan-flexible-ltv-adjustments`, `loan-flexible-liquidations`, 5 VIP-loan reads, `margin-trades`, `margin-liquidation-loan`, `margin-liquidation-loan-repays`, `um-trades`, `cm-trades`, 5 options reads, 2 portfolio-margin reads) + `um-exchange-info` and `cm-exchange-info` (2026-11-27, for the futures hosts' rate limits) + `opt-exchange-info` (2026-11-27, for the options host's).
- Listed by one line: 19 (5 further Portfolio Margin + 4 Portfolio Margin Pro account/balance reads, 3 gift-card reads, 4 mining reads, 2 travel-rule V2 histories, fiat order detail).
- Hosts: `api.binance.com` (90 of the 110), `fapi.binance.com` (6), `dapi.binance.com` (6), `eapi.binance.com` (6), `papi.binance.com` (2; brief).
