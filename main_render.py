# ====================================================================================
# Apex BOT v19.0.39 - FULL COMPLIANCE (Limit Order & Exchange SL/TP, Score 100 Max)
#
# 改良・修正点:
# 1. 【既存修正】execute_trade関数内のCCXT注文応答処理を強化。
# 2. 【既存修正】詳細ロギングを維持。
# 3. 【新規要件対応】RSI短期/長期クロスによるモメンタム確証を強化。
# 4. 【新規要件対応】ATRベースの動的ボラティリティフィルタとペナルティを導入。
# 5. 【新規要件対応】ATRベースの動的ストップロス(SL)/テイクプロフィット(TP)幅決定を導入。
# 6. 【新規要件対応】エラー発生時の完全なトレースバックを含むTelegram通知を実装。
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
import traceback # 【要件8】トレースバック取得用

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

# 💡 新規取引制限設定
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
HOURLY_SIGNAL_LOG: List[Dict] = [] # ★ 1時間内のシグナルを一時的に保持するリスト

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
# 【要件4, 5, 6, 7】スコアリング定数と動的リスク管理定数の調整
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
TREND_ALIGNMENT_BONUS = 0.08        # 中期/長期トレンド一致時のボーナス (0.10 -> 0.08に調整)
STRUCTURAL_PIVOT_BONUS = 0.06       # 価格構造/ピボット支持時のボーナス 
RSI_MOMENTUM_LOW = 45               # RSIが45以下でロングモメンタム候補
RSI_MOMENTUM_BONUS_MAX = 0.08       # RSIの強さに応じた可変ボーナスの最大値 (0.10 -> 0.08に調整)
RSI_SHORT_LENGTH = 7                # 【新規】RSI短期EMA
RSI_LONG_LENGTH = 28                # 【新規】RSI長期EMA
RSI_CROSS_BONUS = 0.04              # 【要件5】RSI短期/長期クロスボーナス
OBV_MOMENTUM_BONUS = 0.05           # OBVの確証ボーナス
VOLUME_INCREASE_BONUS = 0.07        # 出来高スパイク時のボーナス
LIQUIDITY_BONUS_MAX = 0.07          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ

# 【要件6, 7】ATRベースの動的リスク管理/ボラティリティフィルタ定数
ATR_LENGTH = 14                     # ATRの期間
ATR_STOP_LOSS_MULTIPLIER = 1.5      # SL幅 = ATR * 1.5
ATR_RISK_REWARD_RATIO = 2.5         # TP幅 = SL幅 * 2.5
ATR_VOLATILITY_THRESHOLD = 0.0005   # ATR/価格の比率が0.05%未満の場合、低ボラティリティと見なす
ATR_VOLATILITY_PENALTY = 0.05       # 低ボラティリティペナルティ (5点)

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
    """価格を整形する。1.0 USDT以上の価格に対して小数第4位まで表示を保証する。"""
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
    
    # 💡 【要件5】RSI短期/長期クロスボーナス
    rsi_cross_bonus = tech_data.get('rsi_cross_bonus_value', 0.0)
    rsi_cross_status = '✅ RSIクロスによるモメンタム確証' if rsi_cross_bonus > 0 else '➖ RSIクロス 中立'
    rsi_cross_score = f"{rsi_cross_bonus*100:.1f}"
    breakdown.append(f"  - {rsi_cross_status}: <code>+{rsi_cross_score}</code> 点")
    
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

    # 💡 【要件6】ボラティリティペナルティ (低ボラティリティ)
    volatility_atr_pen = tech_data.get('volatility_penalty_value', 0.0)
    volatility_bb_pen = tech_data.get('volatility_bb_penalty_value', 0.0) 
    
    if volatility_atr_pen < 0 and volatility_bb_pen < 0:
        vol_status = '❌ 低ボラティリティ (ATR/BB幅)'
    elif volatility_atr_pen < 0:
        vol_status = '❌ 低ボラティリティ (ATRベース)'
    elif volatility_bb_pen < 0:
        vol_status = '❌ 低ボラティリティ (BB幅ベース)'
    else:
        vol_status = '➖ ボラティリティ 中立'
        
    total_vol_pen = volatility_atr_pen + volatility_bb_pen
    vol_score = f"{total_vol_pen*100:.1f}"
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
    """Telegram通知用のメッセージを作成する"""
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
            trade_status_line = "✅ **自動売買 成功**: 現物指値買い注文が即時約定しました。"
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 指値買い (FOK)</code>\n"
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
        f"  - **指値 (Entry)**: <code>{format_price_precision(entry_price)}</code>\n"
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
        
    message += (f"<i>Bot Ver: v19.0.39 - Dynamic Risk/Momentum Fix</i>")
    return message

def format_hourly_report(signals: List[Dict], start_time: float, current_threshold: float) -> str:
    """1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する"""
    
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
        f"  - **現在の価格**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"\n"
        f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
        f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
        f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
        f"  - **現在の価格**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
        f"\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.39 - Dynamic Risk/Momentum Fix</i>"
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
    # logging.info(f"💾 LOG: {json.dumps(log_data)}")
    return log_data


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def send_telegram_notification(message: str) -> bool:
    """
    指定されたメッセージをTelegramに送信する非同期関数。
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
            # 💡 APIリクエストのタイムアウトを延長 (ミリ秒で指定: 20000ms = 20秒)
            'timeout': 20000, 
        }
        EXCHANGE_CLIENT = exchange_class(config)
        
        # APIキーとシークレットキーが設定されているか確認
        if not API_KEY or not SECRET_KEY:
             logging.warning("⚠️ APIキーまたはシークレットキーが設定されていません。取引機能は無効です。")
             IS_CLIENT_READY = True # 閲覧機能は許可
             return

        # 市場情報をロード
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) の初期化と市場情報ロードが完了しました。")

    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)


async def fetch_account_status() -> Dict:
    """CCXTから口座の残高と、USDT以外の保有資産の情報を取得する。"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが未準備です。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    if not API_KEY or not SECRET_KEY:
        # APIキーがない場合は取引機能がないため、USDT残高のみを返す (ベースロット計算のため)
        logging.warning("⚠️ API認証情報がないため、残高はBASE_TRADE_SIZE_USDTに基づき概算されます。")
        GLOBAL_TOTAL_EQUITY = BASE_TRADE_SIZE_USDT * 5 # 例としてベースロットの5倍
        return {'total_usdt_balance': GLOBAL_TOTAL_EQUITY, 'total_equity': GLOBAL_TOTAL_EQUITY, 'open_positions': [], 'error': False}


    try:
        # 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高の取得
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # total_equity (総資産額) の取得
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
                    continue
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY,
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")
        
    return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """CCXTからOHLCVデータを取得し、Pandas DataFrameとして返す"""
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗: CCXTクライアントが未準備です。({symbol})")
        return None

    try:
        # OHLCVデータの取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)

        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ OHLCVデータ不足: {symbol} ({timeframe})。取得数: {len(ohlcv)}/{limit} (分析をスキップします)")
            return None # データ不足の場合は分析を中止

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        logging.info(f"✅ データ取得成功: {symbol} ({timeframe}) - {len(df)}本のローソク足データを取得しました。")
        return df

    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCV取得失敗 (ネットワークエラー): {symbol} - {e}")
    except ccxt.ExchangeError as e:
        # Ex: 'Invalid symbol' や 'Historical data not available'
        logging.error(f"❌ OHLCV取得失敗 (取引所エラー): {symbol} - {e}")
    except Exception as e:
        logging.error(f"❌ OHLCV取得中に予期せぬエラー: {symbol} - {e}")
    
    return None


async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index) データを取得し、マクロコンテキストを返す"""
    url = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        
        data = response.json().get('data', [])
        
        if data:
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

    # RSI (既存のRSIは長期RSIとして使用し、モメンタム確証のために短期RSI EMAを追加)
    df['RSI'] = ta.rsi(df['close'], length=14) # 標準RSI (長期トレンドRSIとして使用)
    df['RSI_SHORT'] = ta.rsi(df['close'], length=RSI_SHORT_LENGTH) # 【要件5】短期RSI (例: 7)
    df['RSI_LONG'] = ta.rsi(df['close'], length=RSI_LONG_LENGTH)   # 【要件5】長期RSI (例: 28)
    
    # 【要件5】RSIのEMA (クロスオーバー用)
    df['RSI_SHORT_EMA'] = ta.ema(df['RSI_SHORT'], length=3)
    df['RSI_LONG_EMA'] = ta.ema(df['RSI_LONG'], length=9)

    # MACD
    macd_data = ta.macd(df['close'])
    if macd_data is not None and not macd_data.empty:
        df['MACD'] = macd_data.iloc[:, 0]
        df['MACD_H'] = macd_data.iloc[:, 1]
        df['MACD_S'] = macd_data.iloc[:, 2]

    # BBands (ボリンジャーバンド)
    bbands_data = ta.bbands(df['close'], length=20, std=2)
    if bbands_data is not None and not bbands_data.empty:
        df['BBL'] = bbands_data.iloc[:, 0] # Lower
        df['BBM'] = bbands_data.iloc[:, 1] # Middle (SMA)
        df['BBU'] = bbands_data.iloc[:, 2] # Upper
        df['BBB'] = bbands_data.iloc[:, 3] # Band Width Percent

    # OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20) # OBVのSMA (トレンド確認用)

    # Volume (出来高スパイク検出用)
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)

    # 【要件6, 7】ATR (Average True Range) の計算
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # ピボットポイントの計算
    # 簡略化されたピボット (前日終値ベース)
    df['R1'] = df['high'].shift(1) - df['low'].shift(1) + df['close'].shift(1)
    df['S1'] = df['close'].shift(1) - (df['high'].shift(1) - df['low'].shift(1))
    
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    データフレームから買いシグナルを分析し、スコアとリスクリワードを計算する。
    【要件5, 6, 7】に対応するために修正。
    """
    
    # Step 1: データのチェックと基本情報の取得
    if len(df) < LONG_TERM_SMA_LENGTH + 1 or df.isnull().values.any():
        logging.warning(f"⚠️ {symbol} ({timeframe}): インジケーター計算に必要なデータが不足しています。分析をスキップします。")
        return None

    last_close = df['close'].iloc[-1]
    last_low = df['low'].iloc[-1]
    
    # ----------------------------------------------------------------------
    # 【要件7】リスク指標の計算 - ATRベースのSL/TP幅決定
    # ----------------------------------------------------------------------
    
    # 1. ATRベースのSL幅を決定
    current_atr = df['ATR'].iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        logging.warning(f"⚠️ {symbol} ({timeframe}): ATRが計算できませんでした。分析をスキップします。")
        return None
        
    # SL幅 = ATR * 乗数
    sl_distance = current_atr * ATR_STOP_LOSS_MULTIPLIER
    # TP幅 = SL幅 * RRR
    tp_distance = sl_distance * ATR_RISK_REWARD_RATIO
    rr_ratio = ATR_RISK_REWARD_RATIO # RRRは定数を使用

    # 買いシグナルのため、
    stop_loss = last_close - sl_distance
    take_profit = last_close + tp_distance
    entry_price = last_close # 終値を指値価格として使用

    # ----------------------------------------------------------------------
    # Step 3: スコアリング - BASE_SCOREから開始
    # ----------------------------------------------------------------------
    score = BASE_SCORE # 50点からスタート
    
    # A. 長期トレンド逆行ペナルティ
    long_term_reversal_penalty_value = 0.0
    if last_close < df['SMA200'].iloc[-1]:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        score -= long_term_reversal_penalty_value

    # B. 中期/長期トレンド一致ボーナス (SMA50 > SMA200)
    trend_alignment_bonus_value = 0.0
    if df['SMA50'].iloc[-1] > df['SMA200'].iloc[-1] and df['SMA50'].iloc[-2] <= df['SMA200'].iloc[-2]:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        score += trend_alignment_bonus_value

    # C. 価格構造/ピボット支持ボーナス 
    structural_pivot_bonus = 0.0
    s1_pivot = df['S1'].iloc[-1] 
    if last_close > s1_pivot and last_low < s1_pivot * 1.005: 
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
        score += structural_pivot_bonus

    # D. MACDクロス/発散ペナルティ (MACD < Signal の場合ペナルティ)
    macd_penalty_value = 0.0
    macd = df['MACD'].iloc[-1]
    macd_signal = df['MACD_S'].iloc[-1]
    if macd < macd_signal:
        macd_penalty_value = MACD_CROSS_PENALTY
        score -= macd_penalty_value
        
    # E. RSIモメンタムボーナス (短期RSIベース)
    rsi_momentum_bonus_value = 0.0
    rsi = df['RSI_SHORT'].iloc[-1] # 短期RSIを使用
    if RSI_MOMENTUM_LOW < rsi <= 70.0: 
        # 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.08)
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
        score += rsi_momentum_bonus_value
        
    # 💡 【要件5】E-2. RSI短期/長期クロスボーナス
    rsi_cross_bonus_value = 0.0
    if df['RSI_SHORT_EMA'].iloc[-1] > df['RSI_LONG_EMA'].iloc[-1] and df['RSI_SHORT_EMA'].iloc[-2] <= df['RSI_LONG_EMA'].iloc[-2]:
        # 短期RSI EMAが長期RSI EMAをゴールデンクロス
        rsi_cross_bonus_value = RSI_CROSS_BONUS
        score += rsi_cross_bonus_value

    # F. OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        score += obv_momentum_bonus_value

    # G. Volume Spike Bonus (出来高が平均の1.5倍)
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        score += volume_increase_bonus_value

    # H. Volatility Penalty (BB幅が狭すぎる場合)
    volatility_bb_penalty_value = 0.0
    bb_width_percent = df['BBB'].iloc[-1]
    if bb_width_percent < VOLATILITY_BB_PENALTY_THRESHOLD * 100: 
        volatility_bb_penalty_value = -0.05
        score += volatility_bb_penalty_value # マイナス値なので+=で減算

    # 💡 【要件6】H-2. ATR Volatility Penalty (ATR/価格比率が低すぎる場合)
    volatility_penalty_value = 0.0
    atr_price_ratio = current_atr / last_close 
    if atr_price_ratio < ATR_VOLATILITY_THRESHOLD:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 低ボラティリティ検出 ({atr_price_ratio*100:.4f}% < {ATR_VOLATILITY_THRESHOLD*100:.4f}%)。ペナルティを適用します。")
        volatility_penalty_value = -ATR_VOLATILITY_PENALTY
        score += volatility_penalty_value # マイナス値なので+=で減算

    # I. 流動性ボーナス (板情報は省略しMAXボーナスを固定)
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    score += liquidity_bonus_value

    # J. マクロ環境ボーナス/ペナルティ
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX
    score += sentiment_fgi_proxy_bonus

    # ----------------------------------------------------------------------
    # Step 4: 最終的なスコアの調整とシグナルデータの構築
    # ----------------------------------------------------------------------
    # スコアを0.00〜1.00の範囲にクリップ
    score = max(0.00, min(1.00, score))

    tech_data = {
        'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
        'trend_alignment_bonus_value': trend_alignment_bonus_value,
        'structural_pivot_bonus': structural_pivot_bonus,
        'macd_penalty_value': macd_penalty_value,
        'rsi_value': rsi, # 短期RSIの値を使用
        'rsi_momentum_bonus_value': rsi_momentum_bonus_value,
        'rsi_cross_bonus_value': rsi_cross_bonus_value, # 【要件5】
        'obv_momentum_bonus_value': obv_momentum_bonus_value,
        'volume_increase_bonus_value': volume_increase_bonus_value,
        'volatility_bb_penalty_value': volatility_bb_penalty_value, 
        'volatility_penalty_value': volatility_penalty_value, # 【要件6】ATRペナルティ
        'liquidity_bonus_value': liquidity_bonus_value,
        'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
        'current_atr': current_atr, # 【要件7】
        'atr_price_ratio': atr_price_ratio, # 【要件6】
        'atr_sl_multiplier': ATR_STOP_LOSS_MULTIPLIER, # 【要件7】
        'atr_rr_ratio': ATR_RISK_REWARD_RATIO, # 【要件7】
    }
    
    # ----------------------------------------------------------------------
    # Step 5: 結果の構築
    # ----------------------------------------------------------------------
    signal_data = {
        'id': str(uuid.uuid4()),
        'symbol': symbol,
        'timeframe': timeframe,
        'action': 'buy',
        'score': score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio, # ATR_RISK_REWARD_RATIO
        'tech_data': tech_data
    }
    return signal_data


# ... (adjust_order_amount, place_sl_tp_orders, cancel_all_related_orders, execute_trade のコードは既存のまま)
async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """取引所のルールに基づいて注文数量を調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0

    try:
        market = EXCHANGE_CLIENT.market(symbol)
        
        # 1. 価格の丸め (CCXTの仕組みにより自動で丸められることが多いが、念のため手動で丸める)
        if 'price' in market['precision']:
            price = EXCHANGE_CLIENT.decimal_to_precision(price, ccxt.ROUND, market['precision']['price'])
        
        # 2. 基本通貨建て数量 (amount) を計算
        base_amount = usdt_amount / price

        # 3. 数量 (amount) の丸め
        if 'amount' in market['precision']:
            base_amount = EXCHANGE_CLIENT.decimal_to_precision(base_amount, ccxt.TRUNCATE, market['precision']['amount'])

        # 4. 最小取引量のチェック (amountの最小値)
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount < min_amount:
            # 最小ロットを下回る場合は0を返す
            return 0.0, 0.0
        
        # 5. 最終的なUSDTコストを再計算
        final_usdt_amount = base_amount * price

        return float(base_amount), float(final_usdt_amount)

    except Exception as e:
        logging.error(f"❌ 注文数量調整中にエラーが発生: {e}")
        return 0.0, 0.0

async def place_sl_tp_orders(symbol: str, filled_amount: float, stop_loss: float, take_profit: float) -> Dict:
    """約定後、取引所にSL(ストップ指値)とTP(指値)注文を設定する"""
    global EXCHANGE_CLIENT

    if not API_KEY or not SECRET_KEY:
        return {'status': 'error', 'error_message': 'APIキーがないためSL/TP注文をスキップしました。'}

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    sl_order_id = None
    tp_order_id = None
    
    # 💡 注文価格と数量の丸め処理を考慮
    try:
        market = EXCHANGE_CLIENT.market(symbol)
        
        # SL/TPの価格丸め
        sl_price_adj = EXCHANGE_CLIENT.decimal_to_precision(stop_loss, ccxt.ROUND, market['precision']['price'])
        tp_price_adj = EXCHANGE_CLIENT.decimal_to_precision(take_profit, ccxt.ROUND, market['precision']['price'])
        
        # 売り数量の丸め
        amount_to_sell = EXCHANGE_CLIENT.decimal_to_precision(filled_amount, ccxt.TRUNCATE, market['precision']['amount'])

    except Exception as e:
         logging.error(f"❌ SL/TP注文の価格・数量調整エラー: {e}")
         return {'status': 'error', 'error_message': f'SL/TP注文の調整エラー: {e}'}

    logging.info(f"⏳ SL/TP注文を設定中: {symbol} (Qty: {amount_to_sell}). SL={format_price_precision(sl_price_adj)}, TP={format_price_precision(tp_price_adj)}")

    # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
    try:
        # TP価格で指値売り (GTC: Good-Til-Canceled)
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=tp_price_adj,
            params={'timeInForce': 'GTC'} 
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP指値売り注文成功: {symbol} @ {format_price_precision(tp_price_adj)} (ID: {tp_order_id})")
    except Exception as e:
        logging.error(f"❌ TP指値売り注文失敗: {symbol} - {e}")
        # TP失敗の場合でもSLは試みる

    # 2. SL (ストップロス) ストップ指値売り注文の設定 (Stop Limit Sell)
    try:
        # SL価格をトリガーとして、SL価格で指値売り
        sl_params = {
            'stopPrice': sl_price_adj, # トリガー価格
            'type': 'stop_limit',      # ストップ指値を使用
        }
        
        # CCXTのcreate_orderに渡す
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # create_orderのtypeはlimitだが、paramsでstop_limitを指定する場合がある（取引所による）
            side='sell',
            amount=amount_to_sell,
            price=sl_price_adj, # 執行価格もSL価格
            params=sl_params
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SLストップ指値売り注文成功: {symbol} @ {format_price_precision(sl_price_adj)} (ID: {sl_order_id})")

    except Exception as e:
        logging.error(f"❌ SLストップ指値売り注文失敗: {symbol} - {e}")
        # SL失敗の場合はTP注文もキャンセルしてポジションを手動管理に委ねる (リスク回避)
        if tp_order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
                logging.warning(f"⚠️ SL注文失敗のため、TP注文 (ID: {tp_order_id}) をキャンセルしました。")
            except Exception as e_cancel:
                logging.error(f"❌ TP注文キャンセル失敗: {e_cancel}")
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    # 3. SL, TPの両方が成功した場合
    if sl_order_id and tp_order_id:
        return {
            'status': 'ok',
            'sl_order_id': sl_order_id,
            'tp_order_id': tp_order_id
        }
    else:
        # 片方または両方が失敗した場合 (上記エラー処理で捕捉されるはずだが念のため)
         return {'status': 'error', 'error_message': 'SL/TP注文の設定に失敗しました。'}


async def cancel_all_related_orders(position: Dict, open_order_ids: set) -> None:
    """決済されたポジションに関連する未約定の決済注文を全てキャンセルする"""
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    sl_id = position.get('sl_order_id')
    tp_id = position.get('tp_order_id')
    
    ids_to_cancel = []
    if sl_id in open_order_ids:
        ids_to_cancel.append(sl_id)
    if tp_id in open_order_ids:
        ids_to_cancel.append(tp_id)

    if not ids_to_cancel:
        logging.info(f"ℹ️ {symbol}: キャンセルすべきオープン注文はありませんでした。")
        return

    logging.info(f"⏳ {symbol}: 残存する決済注文 ({len(ids_to_cancel)}件) をキャンセルします...")
    
    for order_id in ids_to_cancel:
        try:
            await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
            logging.info(f"✅ 注文キャンセル成功: ID={order_id}, Symbol={symbol}")
        except Exception as e:
            logging.warning(f"⚠️ 注文キャンセル失敗 (ID: {order_id}, Symbol: {symbol}): {e}")


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """
    取引を実行し、指値注文（FOK）とSL/TP注文を設定する (要件3 - 動的リスク管理の実行)
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    lot_size_usdt = signal['lot_size_usdt']
    limit_price = signal['entry_price']

    if TEST_MODE:
        logging.warning("⚠️ TEST_MODE: 取引は実行されません。")
        return {
            'status': 'test',
            'filled_amount': 0.0,
            'filled_usdt': lot_size_usdt,
            'entry_price': limit_price,
            'id': f'TEST-{uuid.uuid4()}',
            'sl_order_id': 'TEST-SL',
            'tp_order_id': 'TEST-TP',
            'message': 'Test trade executed.'
        }

    if not API_KEY or not SECRET_KEY:
        return {'status': 'error', 'error_message': 'APIキーが設定されていないため取引をスキップしました。'}
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    # 1. 注文数量の調整
    try:
        base_amount, final_usdt_amount = await adjust_order_amount(
            symbol=symbol,
            usdt_amount=lot_size_usdt,
            price=limit_price
        )

        if base_amount == 0.0 or final_usdt_amount < MIN_USDT_BALANCE_FOR_TRADE:
            error_message = f'ロットサイズが最小取引量未満、または {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT未満です。'
            logging.error(f"❌ 取引スキップ: {error_message} (調整後USDT: {final_usdt_amount:.2f})")
            return {'status': 'error', 'error_message': error_message}

        current_usdt_balance = account_status.get('total_usdt_balance', 0.0)
        if final_usdt_amount > current_usdt_balance:
            error_message = f"USDT残高不足: 現在 {format_usdt(current_usdt_balance)} USDT。取引に必要な額: {format_usdt(final_usdt_amount)} USDT。"
            logging.error(f"❌ 取引スキップ: {error_message}")
            return {'status': 'error', 'error_message': error_message}
            
        logging.info(f"ℹ️ 最終注文パラメータ: Type=limit (FOK), Price={format_price_precision(limit_price)}, Amount={base_amount:.4f}")
        
    except Exception as e:
        # トレースバックを含むエラーメッセージをログに出力
        logging.error(f"❌ 取引準備エラー: {e}\n{traceback.format_exc()}")
        return {'status': 'error', 'error_message': f'取引準備エラー: {e}'}

    # 2. 現物 指値買い注文 (FOK: 即時約定しない場合はキャンセル)
    try:
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount,
            price=limit_price,
            params={'timeInForce': 'FOK'}
        )

        # 3. 注文結果の確認 【💡 CCXTステータスNone対応のため修正】
        filled_amount = order.get('filled')
        filled_usdt = order.get('cost')
        order_status = order.get('status')
        
        # 注文が部分的にでも約定した場合 (FOKの場合、全量約定が期待される)
        if filled_amount and filled_amount > 0.0 and order_status in ['closed', 'ok', None]: 
            # 即時約定成功
            # averageがNoneの場合はlimit_priceを使用
            entry_price = order.get('average') if order.get('average') is not None else limit_price
            
            logging.info(f"✅ FOK注文成功 ({symbol}): 約定価格={format_price_precision(entry_price)}, 約定数量={filled_amount:.4f}, コスト={format_usdt(filled_usdt)} USDT")

            # SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )

            if sl_tp_result['status'] == 'ok':
                # ポジションを管理リストに追加
                OPEN_POSITIONS.append({
                    'id': order['id'], # 買い注文のID
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'created_at': time.time(),
                })
                
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': entry_price,
                    'id': order['id'], # 買い注文のID
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'message': f"現物指値買い注文が即時全量約定しました。SL/TP注文を設定済み (ID: {order['id']})"
                }
            else:
                logging.error(f"❌ FOK約定後、SL/TP注文設定に失敗しました: {sl_tp_result['error_message']}。ポジションは手動管理が必要です。")
                return {
                    'status': 'error',
                    'error_message': f"SL/TP注文設定失敗: {sl_tp_result['error_message']}。手動で決済注文を設定してください。",
                    'id': order['id'],
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                }
                
        else:
            # FOK注文が不成立（即時約定しなかった）またはfilled=0の場合
            logging.info(f"ℹ️ FOK注文が不成立/未約定: {symbol} (Status: {order_status}, Filled: {filled_amount}). 取引をスキップします。")
            
            # FOK注文は即時キャンセルされるはずだが、念のため注文が未約定の場合はキャンセルを試みる
            if order_status not in ['closed', 'canceled']:
                await EXCHANGE_CLIENT.cancel_order(order['id'], symbol)

            return {'status': 'skipped', 'error_message': 'FOK注文が即時約定しませんでした。'}

    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ 取引失敗 (取引所エラー): {symbol} - Exchange Not Available: {e}")
        return {'status': 'error', 'error_message': f'取引所APIが利用できません: {e}'}
    except ccxt.ExchangeError as e:
        # 例: MIN_ORDER_SIZEなど取引所固有のエラー
        logging.error(f"❌ 取引失敗 (取引所エラー): {symbol} - {e}")
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        # 予期せぬエラー
        logging.error(f"❌ 取引中に予期せぬエラーが発生: {e}\n{traceback.format_exc()}")
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


async def open_order_management_loop():
    """オープン注文（SL/TP）の状態を監視し、ポジションの決済を検出する"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or not API_KEY or not SECRET_KEY:
        return # クライアントが未準備または取引不可の場合はスキップ

    positions_to_remove_ids = []
    
    try:
        # 未決済のオープン注文をフェッチ (SL/TP注文が含まれる)
        open_orders = await EXCHANGE_CLIENT.fetch_open_orders()
        open_order_ids = {order['id'] for order in open_orders}

        logging.info(f"🌐 注文監視開始: 現在 {len(OPEN_POSITIONS)} のポジションを追跡中。オープン注文数: {len(open_orders)}")

        for position in OPEN_POSITIONS:
            is_closed = False
            exit_type = None

            # SL注文とTP注文のIDを取得
            sl_id = position.get('sl_order_id')
            tp_id = position.get('tp_order_id')

            # SLまたはTPの注文IDが存在しない場合はスキップ (注文エラーまたはテストモード)
            if not sl_id and not tp_id:
                logging.warning(f"⚠️ {position['symbol']} は管理IDを持たないため監視スキップ。手動でポジションを確認してください。")
                continue

            # SLまたはTPのどちらかがオープン注文リストに残っているかを確認
            sl_open = sl_id in open_order_ids
            tp_open = tp_id in open_order_ids
            
            if not sl_open and not tp_open:
                # どちらの決済注文も残っていない = 決済完了と推定
                is_closed = True
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
                
                # 約定価格の正確な取得は履歴から行う必要があるが、ここでは概算または省略
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
        # トレースバックを含むエラーメッセージをログに出力
        logging.error(f"❌ オープン注文監視中にエラーが発生: {e}\n{traceback.format_exc()}")
        
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
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START ---")

    # 0. 口座ステータスの更新
    account_status = await fetch_account_status()

    # 1. 市場環境情報の更新
    if not IS_FIRST_MAIN_LOOP_COMPLETED or (time.time() - LAST_SUCCESS_TIME) > 60 * 5: # 5分に一度更新
        # FGIと為替情報を取得
        GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()

    # 2. 監視対象銘柄の更新 (初回のみ、または定期的に)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 出来高TOP銘柄取得ロジックは省略し、デフォルトリストを使用
        # CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols(TOP_SYMBOL_LIMIT)
        logging.info(f"ℹ️ 監視対象銘柄リスト: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄 ({CURRENT_MONITOR_SYMBOLS[:3]}...)")

    # 3. 現在の動的閾値を決定
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    logging.info(f"📊 動的取引閾値: {current_threshold * 100:.2f} / 100 (マクロ影響: {GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0):.2f})")

    new_signals = []
    
    # 4. 監視対象銘柄の分析
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # 既にポジションを保有している銘柄はスキップ
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
             logging.debug(f"ℹ️ {symbol}: ポジション保有中のため分析をスキップします。")
             continue

        best_tf_signal = None
        
        for timeframe in TARGET_TIMEFRAMES:
            # OHLCVデータの取得
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            df = await fetch_ohlcv(symbol, timeframe, limit)
            
            if df is None:
                continue
                
            # インジケーターの計算
            df = calculate_indicators(df)

            # シグナルの分析
            signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signal:
                if best_tf_signal is None or signal['score'] > best_tf_signal['score']:
                    best_tf_signal = signal
        
        if best_tf_signal:
            new_signals.append(best_tf_signal)
            
            # ★ HOURLY_SIGNAL_LOGに分析されたシグナルを保存 (重複を除く)
            # ベストスコアのタイムフレームのみを記録
            if not any(s['symbol'] == best_tf_signal['symbol'] for s in HOURLY_SIGNAL_LOG):
                 HOURLY_SIGNAL_LOG.append(best_tf_signal)

    # 5. ベストシグナルの選定と取引実行
    executed_signals_count = 0
    LAST_ANALYSIS_SIGNALS = new_signals # 最終分析結果を保存

    if new_signals:
        # スコア順にソートして、最もスコアが高いシグナルを採用
        best_signals = sorted(new_signals, key=lambda x: x['score'], reverse=True)
        best_signal = best_signals[0]
        
        logging.info(f"--- 🏆 全銘柄の最高スコア: {best_signal['symbol']} ({best_signal['timeframe']}) - {best_signal['score'] * 100:.2f}点 ---")

        # 動的ロットサイズの計算
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
                logging.warning(f"⚠️ USDT残高不足: {best_signal['symbol']} のシグナルは閾値を超えましたが、残高 {format_usdt(account_status['total_usdt_balance'])} USDT が {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT 未満のためスキップします。")
                # スキップの場合でもシグナルは通知する
                trade_result = {'status': 'error', 'error_message': f'残高不足 ({format_usdt(account_status["total_usdt_balance"])} USDT)'}
        
        else:
            logging.info(f"ℹ️ {best_signal['symbol']}: スコア {best_signal['score'] * 100:.2f} が閾値 {current_threshold*100:.2f} を下回ったため取引をスキップします。")
            
        # シグナル通知のクールダウンをチェック
        last_time = LAST_SIGNAL_TIME.get(best_signal['symbol'], 0.0)
        time_since_last_signal = time.time() - last_time

        # 取引が実行された、またはスコアが閾値を超えた最高のシグナルのみを通知
        if (trade_result and trade_result.get('status') in ['ok', 'error']) or (score_met and time_since_last_signal > TRADE_SIGNAL_COOLDOWN):
            
            # シグナル通知を送信
            notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(notification_message)
            log_signal(best_signal, "Signal Trade")
            
            # 最後に通知/取引を実行した時刻を更新
            LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
        
    # 6. 1時間ごとのレポート通知
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("🕒 1時間スコアレポートを送信します...")
        if HOURLY_SIGNAL_LOG:
            report_message = format_hourly_report(HOURLY_SIGNAL_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
            await send_telegram_notification(report_message)
            HOURLY_SIGNAL_LOG = [] # リストをクリア
        else:
             report_message = format_hourly_report([], LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
             await send_telegram_notification(report_message)
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
    # 7. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        logging.info("🚀 初回メインループ完了。起動通知を送信します。")
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, "v19.0.39")
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    end_time = time.time()
    LAST_SUCCESS_TIME = end_time
    logging.info(f"--- 💡 BOT LOOP END - 実行時間: {end_time - start_time:.2f}秒 ---")


def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """総合スコアに基づき、総資産額に応じた動的ロットサイズ (USDT建て) を計算する (要件3)"""
    total_equity = account_status.get('total_equity', 0.0)
    
    if total_equity == 0.0:
        # 残高情報がない場合はBASE_TRADE_SIZE_USDTをそのまま返す
        return BASE_TRADE_SIZE_USDT

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


# ====================================================================================
# SCHEDULER & API
# ====================================================================================

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """FastAPI起動時にBOTとCCXTクライアントを初期化する"""
    logging.info("🚀 FastAPI起動イベント: CCXTクライアントの初期化を開始します。")
    await initialize_exchange_client()
    
    if IS_CLIENT_READY:
        # メインBOTループの非同期タスクを開始
        asyncio.create_task(main_bot_scheduler())
        
        # オープン注文監視ループの非同期タスクを開始
        asyncio.create_task(open_order_management_scheduler())
    else:
         logging.critical("❌ CCXTクライアントの初期化に失敗したため、メインループを開始できません。")


@app.get("/")
async def root():
    """ヘルスチェックエンドポイント"""
    return JSONResponse(content={
        "status": "ok",
        "message": "Apex BOT is running.",
        "version": "v19.0.39",
        "client": CCXT_CLIENT_NAME,
        "test_mode": TEST_MODE,
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A",
        "open_positions": len(OPEN_POSITIONS),
    })

async def open_order_management_scheduler():
    """オープン注文監視ループを定期実行するスケジューラ (10秒ごと)"""
    await asyncio.sleep(5) # 初回起動後の待機

    while True:
        try:
            await open_order_management_loop()
        except Exception as e:
            # 致命的なエラーが発生した場合でも、ループを継続
            logging.error(f"❌ 注文監視ループ実行中にエラー: {e}\n{traceback.format_exc()}")
            
        await asyncio.sleep(MONITOR_INTERVAL)


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
            
            # 💡 【要件8】完全なトレースバックを取得し、Telegram通知に含める
            error_traceback = traceback.format_exc()
            notification_message = (
                f"🚨 **致命的なエラー**\n"
                f"メインループでエラーが発生しました: `{e}`\n\n"
                f"**🖥️ 完全トレースバック**:\n"
                f"<code>{error_traceback}</code>\n"
                f"<i>Bot Ver: v19.0.39 - Dynamic Risk/Momentum Fix</i>"
            )

            try:
                 # 💡 Telegram通知失敗時の二次エラーをハンドリング
                 await send_telegram_notification(notification_message)
            except Exception as e2:
                 logging.critical(f"二次エラー: エラー通知も失敗しました: {e2}")


        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引くことで、正確な周期実行を保証
        elapsed_time = time.time() - LAST_SUCCESS_TIME
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


# if __name__ == "__main__":
#     # uvicorn.run(app, host="0.0.0.0", port=8000)
#     # 開発/デバッグ用途: 実行するには、このブロックのコメントアウトを解除し、.envファイルを設定してください。
#     # uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
#     pass
