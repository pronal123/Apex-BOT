# ====================================================================================
# Apex BOT v19.0.41 - MARKET ORDER & DYNAMIC SL/TP UPGRADE
#
# 改良・修正点:
# 1. 【成行注文への変更】execute_trade関数で、指値買い(FOK)を成行買い(Market)に変更。
# 2. 【柔軟なSL/TP】generate_signal_and_score関数で、SLを直近の価格構造(BB下限/スイングロー)に基づいて決定。
# 3. 【SL/TP再計算】execute_trade関数で、成行の「実際の約定価格」に基づき、最終的なSL/TPを再計算・設定。
# 4. 【ログ修正】Telegramメッセージとログの注文タイプ表記を更新。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - (%(threadName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT",
    "ZEC/USDT", "PUMP/USDT", "PEPE/USDT", "FARTCOIN/USDT",
    "WLFI/USDT", "PENGU/USDT", "ONDO/USDT", "HBAR/USDT", "TRUMP/USDT",
    "SHIB/USDT", "HYPE/USDT", "LINK/USDT", "ZEC/USDT",
    "VIRTUAL/USDT", "PIPPIN/USDT", "GIGGLE/USDT", "H/USDT", "AIXBT/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
MONITOR_INTERVAL = 10               # オープン注文監視ループの実行間隔 (秒) - 10秒ごと
HOURLY_SCORE_REPORT_INTERVAL = 60 * 60 # ★ 1時間ごとのスコア通知間隔 (60分ごと)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc") # ★デフォルトはmexc
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY") # 環境変数 MEXC_API_KEY を参照
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET") # 環境変数 MEXC_SECRET を参照
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    # 総資産額が不明な場合や、動的ロットの最小値として使用
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 【動的ロット設定】
DYNAMIC_LOT_MIN_PERCENT = 0.10 # 最小ロット (総資産の 10%)
DYNAMIC_LOT_MAX_PERCENT = 0.20 # 最大ロット (総資産の 20%)

# 💡 新規取引制限設定 【★V19.0.33で追加】
MIN_USDT_BALANCE_FOR_TRADE = 20.0 # 新規取引に必要な最小USDT残高 (20.0 USDT)
DYNAMIC_LOT_SCORE_MAX = 0.96   # このスコアで最大ロットが適用される (96点)


# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # ★ 1時間ごとの通知時刻
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} # ★初期値を設定
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (注文IDトラッキング用)
GLOBAL_TOTAL_EQUITY: float = 0.0 # 総資産額を格納するグローバル変数
HOURLY_SIGNAL_LOG: List[Dict] = [] # ★ 1時間内のシグナルを一時的に保持するリスト (V19.0.34で追加)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} # 1m, 5mを含む

# ====================================================================================
# 【★スコアリング定数変更 V19.0.33: 最大スコア100点に正規化 (要件4)】
# (合計最大スコアが1.00になるように調整)
# ====================================================================================
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 

# スコアリングウェイト
BASE_SCORE = 0.50                   # ベースとなる取引基準点 (50点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA

# ペナルティ（マイナス要因）
LONG_TERM_REVERSAL_PENALTY = 0.30   # 長期トレンド逆行時のペナルティを強化
MACD_CROSS_PENALTY = 0.25           # MACDが不利なクロス/発散時のペナルティを強化
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が1%未満

# ボーナス（プラス要因）- 合計0.50点に調整
TREND_ALIGNMENT_BONUS = 0.10        # 中期/長期トレンド一致時のボーナス (元: 0.15)
STRUCTURAL_PIVOT_BONUS = 0.06       # 価格構造/ピボット支持時のボーナス (元: 0.10)
RSI_MOMENTUM_LOW = 45               # RSIが45以下でロングモメンタム候補
RSI_MOMENTUM_BONUS_MAX = 0.10       # RSIの強さに応じた可変ボーナスの最大値 (元: 0.15)
OBV_MOMENTUM_BONUS = 0.05           # OBVの確証ボーナス (元: 0.08)
VOLUME_INCREASE_BONUS = 0.07        # 出来高スパイク時のボーナス (元: 0.10)
LIQUIDITY_BONUS_MAX = 0.07          # 流動性(板の厚み)による最大ボーナス (元: 0.10)
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ (変更なし)

# 市場環境に応じた動的閾値調整のための定数 (変更なし)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.85       
SIGNAL_THRESHOLD_NORMAL = 0.83      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額（ロットサイズ、PnLなど）を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def format_price_precision(price: float) -> str:
    """価格を整形する。1.0 USDT以上の価格に対して小数第4位まで表示を保証する。【★V19.0.32で追加】"""
    if price is None:
        price = 0.0
        
    if price >= 1.0:
        # 1.0 USDT以上の価格は小数第4位まで表示を保証
        return f"{price:,.4f}"
    elif price >= 0.01:
        # 0.01 USDT以上1.0 USDT未満は小数第4位
        return f"{price:.4f}"
    else:
        # 0.01 USDT未満は小数第6位 (精度維持)
        return f"{price:.6f}"

# 💡 修正箇所: スコアに基づいて推定勝率を返す関数 (より細かく、幅広いばらつき)
def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (8段階の細かいばらつき)"""
    # 1.00が最高点。スコアが高いほど勝率が高くなるように8段階で調整
    
    if score >= 0.98:
        return "93%+"
    elif score >= 0.96:
        return "90-93%"
    elif score >= 0.94:
        return "87-90%"
    elif score >= 0.92:
        return "84-87%"
    elif score >= 0.90:
        return "81-84%"
    elif score >= 0.85:
        return "75-81%"
    elif score >= 0.80:
        return "68-75%"
    else:
        # 0.80未満の低スコアの場合
        return "60-68%"

def get_current_threshold(macro_context: Dict) -> float:
    """FGI proxyに基づいて現在の取引閾値を動的に決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    elif fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """シグナルに含まれるテクニカルデータから、スコアの詳細なブレークダウンを文字列として返す"""
    tech_data = signal.get('tech_data', {})
    score = signal['score']
    
    breakdown = []
    
    # ベーススコア
    base_score_line = f"  - **ベーススコア ({signal['timeframe']})**: <code>+{BASE_SCORE*100:.1f}</code> 点"
    breakdown.append(base_score_line)
    
    # 長期トレンド逆行ペナルティ
    lt_reversal_pen = tech_data.get('long_term_reversal_penalty_value', 0.0)
    lt_status = '❌ 長期トレンド逆行' if lt_reversal_pen > 0 else '✅ 長期トレンド一致'
    lt_score = f"{(-lt_reversal_pen)*100:.1f}"
    breakdown.append(f"  - {lt_status} (SMA200乖離): <code>{lt_score}</code> 点")
    
    # 中期トレンドアライメントボーナス
    trend_alignment_bonus = tech_data.get('trend_alignment_bonus_value', 0.0)
    trend_status = '✅ 中期/長期トレンド一致 (SMA50>200)' if trend_alignment_bonus > 0 else '➖ 中期トレンド 中立/逆行'
    trend_score = f"{trend_alignment_bonus*100:.1f}"
    breakdown.append(f"  - {trend_status}: <code>+{trend_score}</code> 点")
    
    # 価格構造/ピボット
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    pivot_status = '✅ 価格構造/ピボット支持' if pivot_bonus > 0 else '➖ 価格構造 中立'
    pivot_score = f"{pivot_bonus*100:.1f}"
    breakdown.append(f"  - {pivot_status}: <code>+{pivot_score}</code> 点")

    # MACDペナルティ
    macd_pen = tech_data.get('macd_penalty_value', 0.0)
    macd_status = '❌ MACDクロス/発散 (不利)' if macd_pen > 0 else '➖ MACD 中立'
    macd_score = f"{(-macd_pen)*100:.1f}"
    breakdown.append(f"  - {macd_status}: <code>{macd_score}</code> 点")

    # RSIモメンタムボーナス (可変)
    rsi_momentum_bonus = tech_data.get('rsi_momentum_bonus_value', 0.0)
    rsi_status = f"✅ RSIモメンタム加速 ({tech_data.get('rsi_value', 0.0):.1f})" if rsi_momentum_bonus > 0 else '➖ RSIモメンタム 中立'
    rsi_score = f"{rsi_momentum_bonus*100:.1f}"
    breakdown.append(f"  - {rsi_status}: <code>+{rsi_score}</code> 点")
    
    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    obv_status = '✅ 出来高/OBV確証' if obv_bonus > 0 else '➖ 出来高/OBV 中立'
    obv_score = f"{obv_bonus*100:.1f}"
    breakdown.append(f"  - {obv_status}: <code>+{obv_score}</code> 点")
    
    # 出来高スパイクボーナス
    volume_increase_bonus = tech_data.get('volume_increase_bonus_value', 0.0)
    volume_status = '✅ 直近の出来高スパイク' if volume_increase_bonus > 0 else '➖ 出来高スパイクなし'
    volume_score = f"{volume_increase_bonus*100:.1f}"
    breakdown.append(f"  - {volume_status}: <code>+{volume_score}</code> 点")

    # 流動性
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    liquidity_status = '✅ 流動性 (板の厚み) 優位'
    liquidity_score = f"{liquidity_bonus*100:.1f}"
    breakdown.append(f"  - {liquidity_status}: <code>+{liquidity_score}</code> 点")

    # マクロ環境
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    macro_status = '✅ FGIマクロ影響 順行' if fgi_bonus >= 0 else '❌ FGIマクロ影響 逆行'
    macro_score = f"{fgi_bonus*100:.1f}"
    breakdown.append(f"  - {macro_status}: <code>{macro_score}</code> 点")

    # ボラティリティペナルティ (低ボラティリティ)
    volatility_pen = tech_data.get('volatility_penalty_value', 0.0)
    vol_status = '❌ 低ボラティリティペナルティ' if volatility_pen < 0 else '➖ ボラティリティ 中立'
    vol_score = f"{volatility_pen*100:.1f}"
    breakdown.append(f"  - {vol_status}: <code>{vol_score}</code> 点")

    return '\n'.join(breakdown)

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **総資産額 (Equity)**: <code>{format_usdt(account_status['total_equity'])}</code> USDT\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
            
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                sl_display = format_price_precision(pos['stop_loss'])
                tp_display = format_price_precision(pos['take_profit'])
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {sl_display} / TP: {tp_display})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

        # CCXTから取得したがボットが管理していないポジション（現物保有資産）
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             ccxt_value = sum(p['usdt_value'] for p in open_ccxt_positions)
             balance_section += (
                 f"  - **現物保有資産**: <code>{len(open_ccxt_positions)}</code> 銘柄 (概算価値: <code>{format_usdt(ccxt_value)}</code> USDT)\n"
             )
        
    balance_section += f"\n"

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。指値とSL/TP注文は取引所側で管理されています。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する【★V19.0.32で価格表示を変更】"""
    global GLOBAL_TOTAL_EQUITY
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # trade_resultから値を取得する場合があるため、get()を使用
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    # 決済通知の場合、positionデータにはtech_dataがないため、空の辞書を渡す
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    failure_section = "" # 💡 取引失敗詳細セクションの追加

    if context == "取引シグナル":
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
        
        # ロットサイズ割合の表示 (金額なのでformat_usdt)
        if GLOBAL_TOTAL_EQUITY > 0 and lot_size >= BASE_TRADE_SIZE_USDT:
            lot_percent = (lot_size / GLOBAL_TOTAL_EQUITY) * 100
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT ({lot_percent:.1f}%)"
        else:
            lot_info = f"<code>{format_usdt(lot_size)}</code> USDT"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {lot_info})"
        
        elif trade_result is None or trade_result.get('status') == 'error':
            error_message = trade_result.get('error_message', 'APIエラー') if trade_result else 'システムエラー'
            trade_status_line = f"❌ **自動売買 失敗**: {error_message}"
            
            # 💡 取引失敗詳細セクションの生成
            failure_section = (
                f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                f"**取引失敗詳細**:\n"
                f"  - ❌ {error_message}\n"
            )

        elif trade_result.get('status') == 'ok':
            # 💡 [変更] 成行注文に合わせたメッセージに変更
            trade_status_line = "✅ **自動売買 成功**: 現物成行買い注文が約定しました。" 
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い (Market)</code>\n" # 💡 [変更]
                f"  - **動的ロット**: {lot_info} (目標)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
                f"  - **SL注文ID**: <code>{trade_result.get('sl_order_id', 'N/A')}</code>\n"
                f"  - **TP注文ID**: <code>{trade_result.get('tp_order_id', 'N/A')}</code>\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        trade_status_line = f"🔴 **ポジション決済**: {exit_type_final} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        # 損益はボット側で計算できないためN/Aとする
        pnl_usdt = trade_result.get('pnl_usdt') if 'pnl_usdt' in trade_result else None
        pnl_rate = trade_result.get('pnl_rate') if 'pnl_rate' in trade_result else None
        filled_amount = trade_result.get('filled_amount', 0.0)

        # SL/TPも trade_resultから取得
        sl_price = trade_result.get('stop_loss', 0.0)
        tp_price = trade_result.get('take_profit', 0.0)
        
        pnl_sign = "✅ 決済完了"
        pnl_line = "  - **損益**: <code>取引所履歴を確認</code>"
        if pnl_usdt is not None and pnl_rate is not None:
             pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
             pnl_line = f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            # 決済価格も高精度表示
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格 (約定価格)**: <code>{format_price_precision(exit_price)}</code>\n"
            # ユーザー要望による追加: 決済セクションに指値価格を追加
            f"  - **指値 SL/TP**: <code>{format_price_precision(sl_price)}</code> / <code>{format_price_precision(tp_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"{pnl_line}"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n" # 最大100点表示
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        # ★ここから価格表示をformat_price_precisionに変更
        f"  - **約定価格 (Entry)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price_precision(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price_precision(take_profit)}</code>\n"
        # リスク・リワード幅（金額）はformat_usdtを維持
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    
    # 💡 失敗セクションがあれば追加
    if failure_section:
        message += failure_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # 💡 スコア詳細ブレークダウンは、シグナル通知のコンテキストでのみ、成功/失敗に関わらず追加する
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.41 - Market Order/Dynamic SLTP</i>")
    return message

def format_hourly_report(signals: List[Dict], start_time: float, current_threshold: float) -> str:
    """1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する (V19.0.34で追加)"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    if not signals_sorted:
        return (
            f"🕒 **Apex BOT 1時間スコアレポート**\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **集計日時**: {start_jst} - {now_jst} (JST)\n"
            f"  - **分析銘柄数**: <code>0</code>\n"
            f"  - **レポート**: 過去1時間以内に分析されたシグナルはありませんでした。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
    
    best_signal = signals_sorted[0]
    worst_signal = signals_sorted[-1]
    
    # 閾値超え銘柄のカウント
    threshold_count = sum(1 for s in signals if s['score'] >= current_threshold)

    message = (
        f"🕒 **Apex BOT 1時間スコアレポート**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **集計日時**: {start_jst} - {now_jst} (JST)\n"
        f"  - **分析銘柄数**: <code>{len(signals)}</code>\n"
        f"  - **閾値超え銘柄**: <code>{threshold_count}</code> ({current_threshold*100:.2f}点以上)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"\n"
        f"🟢 **ベストスコア銘柄 (Top)**\n"
        f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
        f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
        f"  - **参照価格**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"\n"
        f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
        f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
        f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
        f"  - **参照価格**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
        f"\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.41 - Market Order/Dynamic SLTP</i>"
    )
    
    return message

def _to_json_compatible(data: Any) -> Any:
    """JSONシリアライズ可能でない型 (numpy, pandas) を標準のPython型に変換するヘルパー関数"""
    if isinstance(data, (np.ndarray, list)):
        return [_to_json_compatible(item) for item in data]
    elif isinstance(data, (pd.Series, pd.DataFrame)):
        return data.tolist()
    elif isinstance(data, (np.float64, float)):
        return float(data)
    elif isinstance(data, (np.int64, int)):
        return int(data)
    elif isinstance(data, (datetime)):
        return data.isoformat()
    return data

def log_signal(signal: Dict, context: str):
    """シグナルまたは取引結果をJSON形式でログに記録する"""
    log_data = {
        'timestamp_jst': datetime.now(JST).isoformat(),
        'context': context,
        'signal': _to_json_compatible(signal),
        'total_equity': GLOBAL_TOTAL_EQUITY,
        'current_positions_count': len(OPEN_POSITIONS),
    }
    
    # 実際にはここにファイルへの追記ロジックやデータベースへの書き込みロジックが入る
    return log_data


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def send_telegram_notification(message: str) -> bool:
    """
    指定されたメッセージをTelegramに送信する非同期関数。
    NameError解消のため、新たに追加。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定が不足しています。通知をスキップします。")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # URLに含めるパラメータ (HTMLパースモードを使用)
    params = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグ (<code>, <b>など) を使用するためHTMLモード
    }
    
    try:
        # requestsライブラリを使用 (ブロッキングの可能性があるため、本番環境では注意が必要)
        response = requests.post(url, data=params, timeout=10)
        response.raise_for_status()
        
        # Telegram APIの応答をチェック
        if response.json().get('ok'):
            logging.info("✅ Telegram通知を送信しました。")
            return True
        else:
            logging.error(f"❌ Telegram API送信失敗: {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知送信失敗 (ネットワークエラー): {e}")
        return False
    except Exception as e:
        logging.error(f"❌ Telegram通知送信中に予期せぬエラー: {e}")
        return False


async def initialize_exchange_client():
    """CCXTクライアントを初期化し、市場情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    logging.info(f"⏳ CCXTクライアント ({CCXT_CLIENT_NAME}) の初期化を開始します...")
    
    # 以前のインスタンスを閉じる
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()

    try:
        # ccxt_asyncモジュールからクライアントクラスを取得
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())

        # クライアントインスタンスを作成
        config = {
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レートリミットを有効化 (必須)
            'options': {
                'defaultType': 'spot', # 現物取引モード
            },
            # 💡 【修正点】APIリクエストのタイムアウトを延長 (ミリ秒で指定: 20000ms = 20秒)
            'timeout': 20000, 
        }
        EXCHANGE_CLIENT = exchange_class(config)
        
        # 市場情報をロード
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化し、市場情報をロードしました。")
        
        if not API_KEY or not SECRET_KEY:
            logging.warning("⚠️ APIキーまたはシークレットキーが設定されていません。取引機能は無効です。")

    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)


async def fetch_account_status() -> Dict:
    """CCXTから口座の残高と、USDT以外の保有資産の情報を取得する。"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが未準備です。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        # 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高の取得
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # total_equity (総資産額) の取得
        # CCXTの総資産額 (total) を優先的に使用
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance) 
        if GLOBAL_TOTAL_EQUITY == 0.0:
            GLOBAL_TOTAL_EQUITY = total_usdt_balance # フォールバック

        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")
        
        # USDT以外の保有資産の評価
        open_positions = []
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    symbol = f"{currency}/USDT"
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                        if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                            symbol = f"{currency}USDT"
                        else:
                            continue

                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    usdt_value = amount * ticker['last']

                    if usdt_value >= 10: # 10 USDT未満の保有は無視
                        open_positions.append({
                            'symbol': symbol,
                            'amount': amount,
                            'usdt_value': usdt_value
                        })

                except Exception as e:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY,
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

async def fetch_top_symbols(limit: int) -> List[str]:
    """取引所の出来高TOP N銘柄を取得する (現物市場)"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ シンボルリスト取得失敗: CCXTクライアントが未準備です。")
        return DEFAULT_SYMBOLS
    
    # MEXCなどの一部の取引所は `fetchTickers` で `limit` をサポートしない、または出来高でソートしない。
    # そのため、全銘柄を取得後にフィルタリング・ソートを行う。
    
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # フィルタリング: 'USDT'建ての現物取引ペアのみを対象
        usdt_spot_tickers = {
            symbol: ticker for symbol, ticker in tickers.items() 
            if ('/USDT' in symbol or 'USDT' == symbol[-4:]) 
            and 'swap' not in EXCHANGE_CLIENT.markets[symbol]['type']
        }
        
        if not usdt_spot_tickers:
            logging.warning("⚠️ フィルタリングされたUSDT現物シンボルが見つかりませんでした。")
            return DEFAULT_SYMBOLS

        # 出来高 (quoteVolume, USDT) でソート
        # quoteVolumeがない場合はvolumeを使用
        sorted_symbols = sorted(
            usdt_spot_tickers.keys(), 
            key=lambda symbol: usdt_spot_tickers[symbol].get('quoteVolume') or usdt_spot_tickers[symbol].get('volume') or 0.0, 
            reverse=True
        )
        
        # TOP Nを取得
        top_symbols = sorted_symbols[:limit]
        
        logging.info(f"✅ 出来高TOP {len(top_symbols)} 銘柄を取得しました。")
        return list(set(top_symbols + DEFAULT_SYMBOLS)) # DEFAULTとマージし、ユニーク化

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄取得失敗: {e}")
        return DEFAULT_SYMBOLS


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """CCXTからOHLCVデータを取得し、DataFrameとして返す"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
    
    try:
        # fetch_ohlcvはタイムスタンプ、始値、高値、安値、終値、出来高のリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} - {timeframe} のOHLCVデータが空です。")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        if len(df) < limit:
             logging.warning(f"⚠️ {symbol} - {timeframe} のデータが不足しています ({len(df)}/{limit})。")
             # データが不足している場合でも、可能な限り分析を行うためにそのまま返す
        
        return df

    except ccxt.ExchangeError as e:
        # 取引所側でシンボルやタイムフレームがサポートされていない場合
        logging.warning(f"⚠️ {symbol} - {timeframe}: 取引所エラー (OHLCV取得失敗): {e}")
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCVデータ取得中に予期せぬエラー: {e}")
        return None


async def fetch_fgi_data() -> Dict:
    """外部APIからFGI (Fear & Greed Index) データを取得し、-1.0〜1.0に正規化する"""
    FGI_API_URL = "https://api.alternative.me/fng/?limit=1"
    
    try:
        # 💡 APIの応答が速いと想定し、requestsを使用 (非同期タスク内で実行されるため)
        response = requests.get(FGI_API_URL, timeout=5) 
        response.raise_for_status()
        
        data = response.json().get('data')
        
        if data and data[0].get('value'):
            raw_value = int(data[0]['value']) # 0-100
            
            # Raw=0 (Extreme Fear) -> Proxy=-1.0, Raw=100 (Extreme Greed) -> Proxy=1.0
            # Raw=50 (Neutral) -> Proxy=0.0
            fgi_proxy = (raw_value - 50) / 50.0
            
            logging.info(f"✅ FGIデータ取得成功: Raw={raw_value}, Proxy={fgi_proxy:.2f}")
            
            return {
                'fgi_raw_value': raw_value,
                'fgi_proxy': fgi_proxy,
                'forex_bonus': 0.0, # 為替機能は削除
            }

        logging.warning("⚠️ FGIデータ取得失敗: APIデータが空です。")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得失敗 (ネットワークエラー): {e}")
        
    # 失敗時は中立を返す
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}


# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    
    # SMA
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['SMA50'] = ta.sma(df['close'], length=50) # 中期トレンド用に追加

    # RSI
    df['RSI'] = ta.rsi(df['close'], length=14)

    # MACD
    macd_data = df.ta.macd(close='close', fast=12, slow=26, signal=9, append=False)
    # MACDの結果をDataFrameに追加
    # 【MACDキーの再修正】 ユーザーのログに合わせてサフィックス付きのキーを使用する (v19.0.39で修正済み)
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACD_H'] = macd_data['MACDh_12_26_9']
    df['MACD_S'] = macd_data['MACDs_12_26_9']
    
    # Bollinger Bands
    bb_data = df.ta.bbands(close='close', length=20, std=2.0, append=False)
    # 💡 【BBANDSキーの修正】 Key 'BBL_20_2.0' not found エラーに対応するため、一般的なキー名に修正 (v19.0.40で修正)
    df['BBL'] = bb_data['BBL_20_2.0']
    df['BBM'] = bb_data['BBM_20_2.0']
    df['BBU'] = bb_data['BBU_20_2.0']
    df['BBB'] = bb_data['BBB_20_2.0']
    df['BBP'] = bb_data['BBP_20_2.0']
    
    # OBV
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20) # OBVのSMAを追加

    # Volume (出来高平均)
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)
    
    return df

def generate_signal_and_score(
    df: pd.DataFrame, 
    symbol: str, 
    timeframe: str, 
    macro_context: Dict, 
    market_ticker: Dict
) -> Optional[Dict]:
    """
    OHLCVデータから取引シグナルとスコアを生成する。
    ロング（買い）シグナルのみを生成し、ショート（売り）は対象外とする。
    """
    if df is None or len(df) < LONG_TERM_SMA_LENGTH:
        return None # データ不足
        
    df = calculate_indicators(df)
    
    # 最新のローソク足のデータを確認
    current_close = df['close'].iloc[-1]
    
    # シグナルの基本チェック (終値が200SMAを上回っていること)
    if current_close < df['SMA200'].iloc[-1]:
        return None # 長期トレンドが下向き -> ロングシグナルなし
    
    # ====================================================================
    # 1. 価格構造に基づくSL/TPの設定 (柔軟な変動型に変更)
    # ====================================================================
    
    # ATRの計算 (Risk/Rewardのベースとして使用)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    atr = df['ATR'].iloc[-1]
    
    if atr == 0 or pd.isna(atr):
        return None # ATRが計算できない場合はスキップ
        
    # エントリー価格: 現在の終値 (成行注文の参照価格として使用)
    entry_price = current_close
    
    # 💡 [変更] 柔軟なSL/TP: SLを「直近5期間の最安値」または「BB下限」に設定 (構造ベース)
    # 直近の5期間の最安値（現在の足を除く4本。終値-5から終値-1まで）
    recent_low = df['low'].iloc[-5:-1].min()
    bbl_price = df['BBL'].iloc[-1]
    
    # SL価格は、より低い価格（より安全な場所）を選択。
    # BBLは支持線、recent_lowは直近の構造的な安値。
    target_sl_price = min(recent_low, bbl_price) 
    
    # リスク幅の計算
    calculated_risk_amount = entry_price - target_sl_price
    
    # 最小リスク保証: 構造的SLが近すぎる場合、最低1.0 ATRを確保
    MIN_RISK_MULTIPLIER = 1.0 
    
    # 最終的なリスク幅 (calculated_risk_amountまたは1.0 * ATRの大きい方)
    risk_amount = max(calculated_risk_amount, atr * MIN_RISK_MULTIPLIER)
    
    # 最終的なSL価格の決定 (Entryからrisk_amountを引いた価格)
    stop_loss = entry_price - risk_amount 
    
    # リワード比率 (固定: 2.0)
    REWARD_RATIO = 2.0 
    reward_amount = risk_amount * REWARD_RATIO
    take_profit = entry_price + reward_amount
    rr_ratio = REWARD_RATIO
    
    # SLが直近の安値を下回っていないかなどの基本的な健全性チェック (ここでは簡易化のため価格のみ)
    if stop_loss <= 0 or take_profit <= entry_price:
        return None
        
    # ====================================================================
    # 2. スコアリング (最大 1.00)
    # ====================================================================
    total_score = 0.0
    tech_data = {} # スコア内訳用のデータ

    # A. ベーススコア (ロングシグナルであること)
    total_score += BASE_SCORE
    
    # B. 長期トレンド逆行ペナルティ (終値がSMA200から大きく乖離している場合)
    long_term_reversal_penalty_value = 0.0
    # 乖離率の計算
    deviation_percent = (entry_price - df['SMA200'].iloc[-1]) / df['SMA200'].iloc[-1]
    
    # 終値がSMA200から10%以上乖離している場合 (過熱感をペナルティ)
    if deviation_percent > 0.10: 
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
    
    total_score -= long_term_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # C. 中期/長期トレンドアライメントボーナス (SMA50がSMA200を上回っている)
    trend_alignment_bonus_value = 0.0
    if df['SMA50'].iloc[-1] > df['SMA200'].iloc[-1]:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
    total_score += trend_alignment_bonus_value
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value
    
    # D. 価格構造/ピボット支持ボーナス (SL価格がBB下限に近い、または直近の安値付近にある)
    # 簡易化: BB下限(BBL)がSL価格を下回っている場合 (SLが強い支持の下にあると見なす)
    # SLは既にBBL/スイングローを考慮しているため、ここではBBLの下にSLがあることを確認
    structural_pivot_bonus = 0.0
    if stop_loss < df['BBL'].iloc[-1]: 
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # E. MACDクロス/発散ペナルティ (MACD < Signal の場合ペナルティ)
    macd_penalty_value = 0.0
    macd = df['MACD'].iloc[-1]
    macd_signal = df['MACD_S'].iloc[-1]
    
    # MACDがシグナルを下回っている、つまりモメンタムが減速している場合
    if macd < macd_signal:
        macd_penalty_value = MACD_CROSS_PENALTY
    
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (RSIが50に向けて加速)
    rsi_momentum_bonus_value = 0.0
    rsi = df['RSI'].iloc[-1]
    
    if RSI_MOMENTUM_LOW < rsi <= 70.0: # 45から70の範囲を対象
        # 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
        # RSI 50から70の間で線形にボーナスを増加させる
        if rsi > 50.0:
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
    
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    tech_data['rsi_value'] = rsi

    # G. OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    # OBVがSMAを上回り、かつ直前の足でSMAを下回っていた場合（クロス）
    if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
    
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. Volume Spike Bonus
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5:
        # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
    
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value

    # I. Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
    volatility_penalty_value = 0.0
    bb_width_percent = df['BBB'].iloc[-1]
    
    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
        volatility_penalty_value = -0.05 # ペナルティとしてマイナス5点を付与
    
    total_score += volatility_penalty_value # マイナスの値を加算
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    tech_data['bb_width_percent'] = bb_width_percent

    # J. 流動性ボーナス (板情報は省略しMAXボーナスを固定)
    # ★ 簡易実装: 板情報を取得せず、常に最大値を付与すると仮定（ボットのコアロジックをシンプルに保つため）
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX 
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

    # K. マクロ環境スコア (FGI)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # FGI_PROXY_BONUS_MAX (0.05)の範囲で±の点数を加算
    sentiment_fgi_proxy_bonus = (fgi_proxy + forex_bonus) * FGI_PROXY_BONUS_MAX
    total_score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    tech_data['fgi_proxy_value'] = fgi_proxy

    # ====================================================================
    # 3. 最終スコアの調整
    # ====================================================================
    # スコアを0.0から1.00の間にクランプ
    final_score = max(0.0, min(1.0, total_score))

    # ====================================================================
    # 4. シグナルデータの作成
    # ====================================================================
    signal_data = {
        'timestamp': df.index[-1].timestamp(),
        'symbol': symbol,
        'timeframe': timeframe,
        'score': final_score,
        'entry_price': entry_price, # 参照価格 (成行注文の前に計算された価格)
        'stop_loss': stop_loss,     # 参照SL (成行注文の前に計算された価格)
        'take_profit': take_profit, # 参照TP (成行注文の前に計算された価格)
        'rr_ratio': rr_ratio,
        'risk_amount_ref': risk_amount, # 💡 [追加] 成行約定後にSL/TPを再計算するためのリスク幅
        'tech_data': tech_data
    }
    
    return signal_data


def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """総合スコアに基づき、総資産額に応じた動的ロットサイズ (USDT建て) を計算する"""
    global BASE_TRADE_SIZE_USDT
    
    total_equity = account_status.get('total_equity', 0.0)
    
    # 1. 最小ロットと最大ロットの計算
    min_lot = max(BASE_TRADE_SIZE_USDT, total_equity * DYNAMIC_LOT_MIN_PERCENT)
    max_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 2. スコアに基づいた線形補間
    if score >= DYNAMIC_LOT_SCORE_MAX:
        final_lot = max_lot
    elif score <= SIGNAL_THRESHOLD:
        final_lot = min_lot
    else:
        # スコア範囲 (SIGNAL_THRESHOLD から DYNAMIC_LOT_SCORE_MAX) で線形に増加
        score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
        lot_range = max_lot - min_lot
        
        if score_range > 0:
            final_lot = min_lot + lot_range * ((score - SIGNAL_THRESHOLD) / score_range)
        else:
            final_lot = min_lot
            
    # 💡 ロギング強化: 動的ロットサイズの詳細
    logging.info(
        f"💰 ロット計算: Score={score*100:.2f}. "
        f"Equity={format_usdt(total_equity)} USDT. "
        f"Min/Max Lot={format_usdt(min_lot)}/{format_usdt(max_lot)} USDT. "
        f"最終ロットサイズ: {format_usdt(final_lot)} USDT"
    )
    
    return final_lot

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小数量、最小ロットサイズ、数量の精度に従って注文数量を調整する。
    Returns: (base_amount, final_usdt_amount)
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0
        
    market = EXCHANGE_CLIENT.market(symbol)

    # 1. Base amount の計算 (購入数量)
    base_amount_unrounded = usdt_amount / price

    # 2. 数量の精度 (amount_precision) と最小数量 (min_amount) の取得
    amount_precision = market['precision']['amount'] if market and market['precision'] else 4 # 精度 (小数点以下の桁数)
    min_amount = market['limits']['amount']['min'] if market and market['limits'] else 0.0001
    
    # 3. 数量の丸め (Truncation: 最小数量が0でない場合に適用)
    if amount_precision is not None:
        # amount_precisionに基づいて丸める (通常は四捨五入ではなく切り捨てまたは桁指定)
        # ここではPythonのmathモジュールを使用して切り捨てる
        # 例: 4桁の場合、10**4 = 10000 をかけて整数にし、切り捨ててから10000で割る
        power_of_ten = 10 ** amount_precision
        base_amount_rounded = math.floor(base_amount_unrounded * power_of_ten) / power_of_ten
    else:
        base_amount_rounded = base_amount_unrounded

    # 4. 最小数量チェック
    final_amount = base_amount_rounded
    if final_amount < min_amount:
        logging.warning(f"⚠️ {symbol} - 数量 {final_amount:.8f} は最小数量 {min_amount:.8f} を満たしません。")
        final_amount = 0.0

    # 5. 最終USDT金額の計算 (約定金額)
    final_usdt_amount = final_amount * price
    
    return final_amount, final_usdt_amount


async def place_sl_tp_orders(symbol: str, filled_amount: float, stop_loss: float, take_profit: float) -> Dict:
    """
    約定した現物ポジションに対して、SL(ストップ指値)とTP(指値)の売り注文を設定する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です'}
        
    if filled_amount <= 0.0:
        return {'status': 'error', 'error_message': '約定数量がゼロです'}

    sl_order_id = None
    tp_order_id = None
    
    logging.info(f"⏳ SL/TP注文を設定中: {symbol} (Qty: {filled_amount:.4f}). SL={format_price_precision(stop_loss)}, TP={format_price_precision(take_profit)}")

    # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
    try:
        # 数量の丸め (ここでは価格はTP価格を使用)
        amount_to_sell = filled_amount
        
        # TP価格で指値売り
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=take_profit,
            params={'timeInForce': 'GTC'} # GTC (Good-Til-Canceled)
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP指値売り注文成功: {symbol} @ {format_price_precision(take_profit)} (ID: {tp_order_id})")

    except Exception as e:
        logging.error(f"❌ TP指値売り注文設定失敗: {symbol} - {e}")
        # TP設定失敗時は、SLを設定せず、即座にポジションをクローズ（リスクを負わない）
        return {'status': 'error', 'error_message': f'TP注文設定失敗: {e}'}

    # 2. SL (ストップ指値) 売り注文の設定 (Stop Limit Sell)
    try:
        # SLトリガー価格と指値価格を設定。指値価格はトリガー価格より少し低く設定するのが一般的 (例: 0.1%下)
        stop_price = stop_loss
        # 💡 MEXCでは 'Stop-Limit' が使えない場合があるため、ここではCCXT標準の 'stop'/'stop_loss' を試行
        
        # 指値価格はトリガー価格と同一、またはわずかに低い価格
        limit_price = stop_price * 0.999 # SL価格より0.1%下の指値価格
        
        amount_to_sell = filled_amount

        # ストップ指値売り注文 (Stop Limit Sell)
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # CCXT標準では'limit'を使い、paramsでstopPriceを指定する方式が多い
            side='sell',
            amount=amount_to_sell,
            price=limit_price, # 実際に取引所に出される指値価格
            params={
                'stopPrice': stop_price, # ストップが発動する価格 (CCXT標準形式)
                'timeInForce': 'GTC',
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SLストップ指値売り注文成功: {symbol} (Trigger: {format_price_precision(stop_price)}, Limit: {format_price_precision(limit_price)}) (ID: {sl_order_id})")

    except Exception as e:
        logging.error(f"❌ SLストップ指値売り注文設定失敗: {symbol} - {e}")
        # SL設定失敗時は、TP注文をキャンセルし、即座にポジションをクローズ（リスクを負わない）
        if tp_order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ SL失敗のため、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as cancel_e:
                logging.error(f"❌ TPキャンセル失敗: {cancel_e}")
        
        return {'status': 'error', 'error_message': f'SL注文設定失敗: {e}'}

    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id
    }


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """シグナルに基づき、現物成行買い注文を発注し、成功すればSL/TP注文を設定する"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if TEST_MODE or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'テストモードまたはクライアント未準備'}
    
    symbol = signal['symbol']
    # 成行注文ではentry_priceは参照用
    entry_price_ref = signal['entry_price'] 
    
    # 動的ロットサイズの決定 (USDT建て)
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナル情報にロットサイズを追加
    
    # 最小取引残高のチェック
    if account_status['total_usdt_balance'] < MIN_USDT_BALANCE_FOR_TRADE:
        return {'status': 'error', 'error_message': f'残高不足: {format_usdt(account_status["total_usdt_balance"])} USDT'}
    
    # 最終的な注文数量とUSDT建て金額を計算 (参照価格 entry_price_ref を使用)
    base_amount_to_buy, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price_ref)

    if base_amount_to_buy <= 0.0:
        return {'status': 'error', 'error_message': '取引所ルールに基づき数量がゼロになりました'}

    # 💡 [変更] 成行注文である旨をログに記録
    logging.info(f"⏳ 現物成行買い注文を発注: {symbol} (Qty: {base_amount_to_buy:.4f}, USDT: {format_usdt(final_usdt_amount)})")
    
    try:
        # 💡 [変更] 成行買い注文 (Market Buy) を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market', # ★ 成行買い(market)に変更
            side='buy',
            amount=base_amount_to_buy,
            # price, params={'timeInForce': 'FOK'} は削除
        )
        
        filled_amount = order.get('filled')
        filled_usdt = order.get('cost') 

        # 注文が約定したかを確認 (成行なので約定することが期待される)
        if filled_amount and filled_amount > 0.0:
            # 約定価格 (average) を取得
            entry_price = order.get('average') 
            
            logging.info(f"✅ 成行注文成功 ({symbol}): 約定価格={format_price_precision(entry_price)}, 約定数量={filled_amount:.4f}, コスト={format_usdt(filled_usdt)} USDT")
            
            # 💡 [追加] 柔軟なSL/TP価格を、**実際の約定価格**で再計算
            risk_amount = signal.get('risk_amount_ref', 0.0)
            REWARD_RATIO = 2.0 # generate_signal_and_scoreの定数と同じ

            # 実際の約定価格に基づいてSL/TPを再計算
            stop_loss_final = entry_price - risk_amount
            take_profit_final = entry_price + (risk_amount * REWARD_RATIO)
            
            # SL/TP注文の設定
            # 💡 [変更] 再計算された最終SL/TP価格を渡す
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=stop_loss_final,
                take_profit=take_profit_final
            )

            if sl_tp_result['status'] == 'ok':
                # ポジションをグローバルリストに追加
                OPEN_POSITIONS.append({
                    'id': str(uuid.uuid4()), # ボットが管理するユニークID
                    'symbol': symbol,
                    'timeframe': signal['timeframe'],
                    'entry_price': entry_price,
                    # 💡 [変更] 最終的なSL/TP価格を記録
                    'stop_loss': stop_loss_final, 
                    'take_profit': take_profit_final,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'entry_time': time.time(),
                })
                
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': entry_price,
                    'id': order['id'], # 買い注文のID
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'message': f"現物成行買い注文が約定しました。SL/TP注文を設定済み (ID: {order['id']})"
                }
            else:
                logging.error("❌ 成行約定後のSL/TP注文設定に失敗しました。ポジションは手動でクローズしてください。")
                
                # SL/TP設定に失敗した場合、リスク回避のため即座にポジションを成行でクローズする
                try:
                    await EXCHANGE_CLIENT.create_order(symbol, 'market', 'sell', filled_amount)
                    logging.warning(f"⚠️ SL/TP設定失敗のため、{symbol} のポジションを即時成行でクローズしました。")
                except Exception as close_e:
                    logging.critical(f"❌ SL/TP設定失敗後の強制クローズも失敗: {close_e}")

                return {'status': 'error', 'error_message': f'成行約定後にSL/TP設定に失敗: {sl_tp_result["error_message"]}'}

        elif order.get('status') in ['canceled', 'rejected'] or (order.get('status') is None and (filled_amount is None or filled_amount == 0.0)):
            # 成行注文は即座に約定することが期待されるため、このパスは主にAPIエラーを示唆する
            error_message = '成行買い注文が約定されませんでした (APIエラーまたは取引所のエラー)。'
            logging.error(f"❌ 警告: {error_message} - CCXT Status: {order.get('status')}")
            return {'status': 'error', 'error_message': error_message}
            
        else:
            # その他の未約定ステータス
            error_message = f'成行注文が予期せぬステータスになりました (Status: {order.get("status")})。'
            logging.error(f"❌ 警告: {error_message}")
            return {'status': 'error', 'error_message': error_message}

    except ccxt.InsufficientFunds as e:
        error_message = f'残高不足: {e}'
        logging.error(f"❌ {symbol} - 成行注文失敗: {error_message}")
        return {'status': 'error', 'error_message': error_message}
    except ccxt.InvalidOrder as e:
        error_message = f'不正な注文: {e}'
        logging.error(f"❌ {symbol} - 成行注文失敗: {error_message}")
        return {'status': 'error', 'error_message': error_message}
    except Exception as e:
        error_message = f'予期せぬAPIエラー: {e}'
        logging.error(f"❌ {symbol} - 成行注文失敗: {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}


async def cancel_all_related_orders(position: Dict, open_order_ids: List[str]):
    """特定のポジションに関連するすべての決済注文をキャンセルする"""
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    
    # SL注文のキャンセル
    if position['sl_order_id'] in open_order_ids:
        try:
            await EXCHANGE_CLIENT.cancel_order(position['sl_order_id'], symbol)
            logging.info(f"✅ SL注文 (ID: {position['sl_order_id']}) をキャンセルしました。")
        except Exception as e:
            logging.warning(f"⚠️ SL注文 (ID: {position['sl_order_id']}) のキャンセルに失敗: {e}")

    # TP注文のキャンセル
    if position['tp_order_id'] in open_order_ids:
        try:
            await EXCHANGE_CLIENT.cancel_order(position['tp_order_id'], symbol)
            logging.info(f"✅ TP注文 (ID: {position['tp_order_id']}) をキャンセルしました。")
        except Exception as e:
            logging.warning(f"⚠️ TP注文 (ID: {position['tp_order_id']}) のキャンセルに失敗: {e}")


async def open_order_management_loop():
    """オープン注文（SL/TP）の状態を監視し、決済されたポジションをクローズ処理するループ (10秒ごと)"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT, GLOBAL_MACRO_CONTEXT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("❌ 注文監視スキップ: CCXTクライアントが未準備です。")
        return

    positions_to_remove_ids = []
    
    try:
        # 💡 【修正点】保有ポジションのシンボルのみを対象にオープン注文を取得する
        symbols_with_open_positions = [pos['symbol'] for pos in OPEN_POSITIONS]
        unique_symbols = list(set(symbols_with_open_positions))
        
        if not unique_symbols:
            # 保有ポジションがなければ、処理をスキップ
            logging.debug("ℹ️ 監視対象のオープンポジションがありません。")
            return

        # 💡 【MEXC対応】 fetchOpenOrders()は、シンボルなしでの全件取得をサポートしない取引所があるため、
        #    シンボルごとにループを回し、オープン注文を取得する。
        
        all_open_orders = []
        for symbol in unique_symbols:
            try:
                # fetchOpenOrdersはシンボルを必須とする取引所が多い (MEXC含む)
                orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol)
                all_open_orders.extend(orders)
            except Exception as e:
                logging.error(f"❌ {symbol} のオープン注文取得に失敗: {e}")
                
        # 注文IDのリストを作成
        open_order_ids = [order['id'] for order in all_open_orders]
        
        logging.info(f"🔍 注文監視: {len(OPEN_POSITIONS)} ポジションを監視中。オープン注文総数: {len(open_order_ids)}")

        # 各ポジションの状態をチェック
        for position in OPEN_POSITIONS:
            is_closed = False
            exit_type = None
            
            # SL/TP注文が取引所に存在するか確認
            sl_open = position['sl_order_id'] in open_order_ids
            tp_open = position['tp_order_id'] in open_order_ids
            
            if not sl_open and not tp_open:
                # 両方の注文が取引所から消滅している = 決済が完了した
                is_closed = True
                # 決済の種類は不明（SL/TPどちらか、または手動）
                exit_type = "SL/TP (取引所決済完了)" 
                logging.info(f"🔴 決済検出: {position['symbol']} - SL/TP注文が取引所から消滅。決済完了と見なします。")

            elif sl_open and tp_open:
                # 決済注文が両方とも残っている = ポジションオープン中
                logging.debug(f"ℹ️ {position['symbol']} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
                pass
            else:
                # 片方のみが残っている場合（取引所の自動キャンセルに失敗）は、一旦オープン中として扱う
                logging.warning(f"⚠️ {position['symbol']} は片方の決済注文が消滅 (SL:{sl_open}, TP:{tp_open})。自動キャンセル失敗の可能性あり。")
                pass


            if is_closed:
                positions_to_remove_ids.append(position['id'])
                
                # 約定価格は履歴から取得が必要だが、ここでは簡略化のため0.0とする
                closed_result = {
                    'symbol': position['symbol'],
                    'entry_price': position['entry_price'],
                    'stop_loss': position['stop_loss'],
                    'take_profit': position['take_profit'],
                    'exit_price': 0.0, # 約定価格は履歴から取得が必要だが、ここでは省略
                    'filled_amount': position['filled_amount'],
                    'exit_type': exit_type,
                    'pnl_usdt': None, # PnLは履歴から取得が必要
                    'pnl_rate': None,
                }

                # 通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                notification_message = format_telegram_message(closed_result, "ポジション決済", current_threshold, closed_result, exit_type)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "Position Exit")

                # 残った未約定注文をキャンセル (念のため)
                await cancel_all_related_orders(position, open_order_ids)


    except Exception as e:
        logging.error(f"❌ オープン注文監視中にエラーが発生: {e}")
        
    finally:
        # 監視リストから決済されたポジションを削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    # 0. アカウントステータスを取得 (ログに総資産額を表示するため)
    account_status = await fetch_account_status()
    
    # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() # FGIの値をスコアリングに反映する準備
    
    macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0) * FGI_PROXY_BONUS_MAX) * 100
    
    # 動的取引閾値の取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    logging.info(f"📊 動的取引閾値: {current_threshold*100:.2f} / 100 (マクロ影響: {macro_influence_score:.2f} 点)")

    # 2. 監視対象銘柄の更新 (初回実行時、またはSKIP_MARKET_UPDATEがFalseの場合)
    if not IS_FIRST_MAIN_LOOP_COMPLETED and not SKIP_MARKET_UPDATE:
        CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols(TOP_SYMBOL_LIMIT)
        logging.info(f"✅ 監視対象銘柄リストを更新しました。合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
    elif not IS_FIRST_MAIN_LOOP_COMPLETED:
         logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。デフォルトの監視銘柄リストを使用します。")
    
    # 3. 全ての監視銘柄・タイムフレームで分析を実行
    all_signals: List[Dict] = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # クールダウンチェック: 過去2時間以内にシグナルが発動していないか
        if symbol in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[symbol] < TRADE_SIGNAL_COOLDOWN):
            logging.debug(f"ℹ️ {symbol} はクールダウン中です。スキップします。")
            continue
            
        # ポジション保有中の銘柄はスキップ
        if any(pos['symbol'] == symbol for pos in OPEN_POSITIONS):
            logging.debug(f"ℹ️ {symbol} はポジション保有中です。スキップします。")
            continue
            
        
        # 💡 全てのタイムフレームでデータが揃わないと分析に進めないようにする
        ohlcv_data: Dict[str, pd.DataFrame] = {}
        market_ticker = None

        try:
            # 必要なOHLCVデータを一括で取得
            tasks = [fetch_ohlcv(symbol, tf, limit) 
                     for tf, limit in REQUIRED_OHLCV_LIMITS.items()]
            
            results = await asyncio.gather(*tasks)
            
            for i, tf in enumerate(REQUIRED_OHLCV_LIMITS.keys()):
                df = results[i]
                limit = REQUIRED_OHLCV_LIMITS[tf]
                
                # 必要なデータ長を満たしているかチェック
                if df is not None and len(df) >= limit:
                    ohlcv_data[tf] = df
                else:
                    # 1つでもデータが不足していれば、その銘柄の分析をスキップ
                    logging.warning(f"⚠️ {symbol}: タイムフレーム {tf} のデータが不足しています。分析をスキップ。")
                    break
            
            # 全てのデータが揃っている場合のみ続行
            if len(ohlcv_data) != len(REQUIRED_OHLCV_LIMITS):
                continue 
            
            # 最新の価格を取得 (OHLCVが最新価格と異なる可能性があるため)
            market_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)


        except Exception as e:
            logging.error(f"❌ {symbol} のデータ取得中にエラー: {e}")
            continue

        
        # 全てのOHLCVデータが揃っている場合、各タイムフレームでシグナル生成
        for tf, df in ohlcv_data.items():
            
            # 【注意】スコアリングは1つのタイムフレームのデータとマクロ環境から生成する
            signal = generate_signal_and_score(
                df=df, 
                symbol=symbol, 
                timeframe=tf, 
                macro_context=GLOBAL_MACRO_CONTEXT, 
                market_ticker=market_ticker
            )
            
            if signal and signal['score'] >= 0.50: # ベーススコア以上のシグナルのみを記録
                all_signals.append(signal)
                HOURLY_SIGNAL_LOG.append(signal) # 1時間レポート用にログに追加

    
    # 4. 最もスコアの高いシグナルを選定
    all_signals.sort(key=lambda s: s['score'], reverse=True)
    
    LAST_ANALYSIS_SIGNALS = all_signals[:TOP_SIGNAL_COUNT]
    
    if LAST_ANALYSIS_SIGNALS:
        best_signal = LAST_ANALYSIS_SIGNALS[0]
        logging.info(f"🏆 最優秀シグナル: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score'] * 100:.2f}")
    else:
        best_signal = None
        logging.info("ℹ️ 今回のループで有効な取引シグナルは見つかりませんでした。")

    # 5. 取引実行
    if best_signal and not TEST_MODE:
        
        # 動的ロットの計算 (取引失敗時にも通知にロット情報を表示するため、ここで計算)
        lot_size_usdt = calculate_dynamic_lot_size(best_signal['score'], account_status)
        best_signal['lot_size_usdt'] = lot_size_usdt 

        # 取引シグナルが閾値を超えているか
        score_met = best_signal['score'] >= current_threshold
        
        # 最低USDT残高があるか
        min_balance_met = account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE

        # 取引実行結果を格納する辞書を初期化
        trade_result = None

        if score_met:
            if min_balance_met:
                logging.info(f"🔥 取引シグナル発動: {best_signal['symbol']} - スコア {best_signal['score'] * 100:.2f} >= 閾値 {current_threshold*100:.2f}。取引を実行します。")
                
                # 取引の実行
                trade_result = await execute_trade(best_signal, account_status)

            else:
                # スコアは満たしたが、残高不足
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                trade_result = {'status': 'error', 'error_message': error_message}
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")


        # 6. Telegram通知
        if trade_result and trade_result.get('status') == 'ok':
            # 取引成功
            # 💡 [変更] trade_resultから最終SL/TPを取得するため、signalを更新
            best_signal['stop_loss'] = trade_result.get('stop_loss', best_signal['stop_loss'])
            best_signal['take_profit'] = trade_result.get('take_profit', best_signal['take_profit'])
            
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "Trade Executed")
            LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # クールダウン開始

        elif trade_result and trade_result.get('status') == 'error':
             # 取引失敗 (残高不足、APIエラーなど)
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "Trade Failed")
            # 失敗してもクールダウンは適用しない

    elif best_signal and TEST_MODE:
         # テストモードでシグナルが発生した場合
        lot_size_usdt = calculate_dynamic_lot_size(best_signal['score'], account_status)
        best_signal['lot_size_usdt'] = lot_size_usdt
        
        if best_signal['score'] >= current_threshold:
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, {'status': 'info', 'error_message': 'TEST_MODE'})
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "Test Signal Generated")
            LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # クールダウン開始

    # 7. 初回起動完了通知 (一度だけ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動完了通知を送信
        startup_message = format_startup_message(
            account_status=account_status,
            macro_context=GLOBAL_MACRO_CONTEXT,
            monitoring_count=len(CURRENT_MONITOR_SYMBOLS),
            current_threshold=current_threshold,
            bot_version="v19.0.41"
        )
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 8. 1時間ごとのスコアレポート
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        if HOURLY_SIGNAL_LOG:
            report_message = format_hourly_report(HOURLY_SIGNAL_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
            await send_telegram_notification(report_message)
        
        # ログをクリアし、通知時刻を更新
        HOURLY_SIGNAL_LOG = []
        LAST_HOURLY_NOTIFICATION_TIME = time.time()


    end_time = time.time()
    elapsed = end_time - start_time
    logging.info(f"--- 💡 BOT LOOP END - 実行時間: {elapsed:.2f} 秒 ---")


async def main_bot_scheduler():
    """メインBOTループを定期実行するスケジューラ (1分ごと)"""
    # 初回起動後の待機時間を考慮し、初回は即座に実行を試みる
    await asyncio.sleep(5) 
    
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            # 致命的なエラーが発生した場合でも、ループを継続するためにエラーをログに記録し、待機時間を経て再試行
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            try:
                 # 💡 Telegram通知失敗時の二次エラーをハンドリング
                 await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")
            except Exception:
                 pass
                 
        await asyncio.sleep(LOOP_INTERVAL)


async def open_order_management_scheduler():
    """オープン注文監視ループを定期実行するスケジューラ (10秒ごと)"""
    # メインループの起動を待ってから開始
    await asyncio.sleep(10)
    
    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            logging.error(f"❌ 注文監視ループ実行中にエラーが発生: {e}", exc_info=True)
        
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI & ENTRY POINT
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI(title="Apex BOT API", version="v19.0.41")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインのタスクを開始する"""
    logging.info("🚀 BOTの起動処理を開始します...")
    
    # CCXTクライアントの初期化
    await initialize_exchange_client()
    
    # メインBOTループの非同期タスクを開始
    asyncio.create_task(main_bot_scheduler())
    
    # オープン注文監視ループの非同期タスクを開始
    asyncio.create_task(open_order_management_scheduler())


@app.get("/status")
async def get_status():
    """BOTの現在のステータスを返すAPIエンドポイント"""
    return JSONResponse(content={
        "status": "running" if IS_CLIENT_READY else "initializing",
        "version": "v19.0.41",
        "exchange": CCXT_CLIENT_NAME.upper(),
        "test_mode": TEST_MODE,
        "last_analysis_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A",
        "total_equity_usdt": GLOBAL_TOTAL_EQUITY,
        "open_positions_count": len(OPEN_POSITIONS),
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "best_signals": LAST_ANALYSIS_SIGNALS,
    })

# 開発用エントリーポイント (デバッグやローカル実行用)
if __name__ == "__main__":
    # uvicornサーバーを起動
    uvicorn.run(app, host="0.0.0.0", port=8000)
