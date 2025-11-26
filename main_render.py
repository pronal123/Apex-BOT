# ====================================================================================
# Apex BOT v19.0.53 (Patched) - FEATURE: Periodic SL/TP Re-Placing for Unmanaged Orders
#
# 改良・修正点:
# 1. 【SL/TP再設定】open_order_management_loop関数内に、SLまたはTPの注文が片方または両方欠けている場合に、
#    残っている注文をキャンセルし、SL/TP注文を再設定するロジックを追加。
# 2. 【IOC失敗診断維持】v19.0.52で追加したIOC失敗時診断ログを維持。
# 3. 【レポート表示修正】Hourly Reportの分析対象数計算ロジックを修正 (v19.0.53-p1)
# 4. 【通知強化】取引シグナル通知に推定損益(USDT)を表示する機能を追加 (v19.0.53-p1)
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
HOURLY_ATTEMPT_LOG: Dict[str, str] = {} # ★ 1時間内の分析試行を保持するリスト (Symbol: Reason)

# ★ 新規追加: ボットのバージョン (v19.0.53-p1: レポート修正＆推定損益表示版)
BOT_VERSION = "v19.0.53-p1"

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン（使われていないが定数として残す）
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

# 市場環境に応じた動的閾値調整のための定数 (★ V19.0.51 修正箇所: 閾値を86点ベースに設定)
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.86       # 88.00点 (リスクオフ時は厳しく)
SIGNAL_THRESHOLD_NORMAL = 0.84      # 86.00点 (ベースライン)
SIGNAL_THRESHOLD_ACTIVE = 0.80      # 83.00点 (リスクオン時は緩く)      

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

# 💡 【★V19.0.41 修正箇所】 スコアに基づいて推定勝率を返す関数 (より細かく、最低勝率0%対応)
def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (より細かい段階と最低勝率0%に対応)"""
    
    # 1. スコアと勝率の基準点を設定
    # 0.60点 (60点) を勝率 0% のベースラインとする
    min_score = 0.60
    max_score = 1.00
    min_win_rate = 0.0 # 0%
    max_win_rate = 95.0 # 95%
    
    # 2. スコアを勝率パーセンテージに変換 (線形近似を使用)
    if score <= min_score:
        base_rate = 0.0
    elif score >= max_score:
        base_rate = max_win_rate # 95.0
    else:
        # 線形補間: V = V_min + (V_max - V_min) * ((S - S_min) / (S_max - S_min))
        ratio = (score - min_score) / (max_score - min_score)
        base_rate = min_win_rate + (max_win_rate - min_win_rate) * ratio
            
    # 3. 段階に分割して表示 (より細かい粒度 5%刻み)
    if base_rate >= 95:
        return "95%+"
    elif base_rate >= 90:
        return "90-95%"
    elif base_rate >= 85:
        return "85-90%"
    elif base_rate >= 80:
        return "80-85%"
    elif base_rate >= 75:
        return "75-80%"
    elif base_rate >= 70:
        return "70-75%"
    elif base_rate >= 65:
        return "65-70%"
    elif base_rate >= 60:
        return "60-65%"
    elif base_rate >= 50:
        return "50-60%"
    elif base_rate >= 30:
        return "30-50%"
    elif base_rate > 0:
        # 0%より大きく、30%未満
        return "1-30%"
    else:
        return "0%"

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
    bot_version: str # この引数はそのまま維持
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


# ★ v19.0.46 修正点: exit_typeを分離し、bot_versionの代わりにグローバル定数 BOT_VERSION を使用するように修正
def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する"""
    global GLOBAL_TOTAL_EQUITY, BOT_VERSION
    
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

    # 💡 【追加】推定損益の計算ロジック
    est_pnl_line = ""
    if context == "取引シグナル" and entry_price > 0:
        # 数量(Amount)の確定: 約定していればその数量、なければシグナルの想定数量を使用
        calc_amount = 0.0
        if trade_result and trade_result.get('filled_amount', 0.0) > 0:
            calc_amount = trade_result['filled_amount']
        else:
            # まだ約定していない、またはテストモードの場合は想定ロットサイズから計算
            lot_usdt = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
            calc_amount = lot_usdt / entry_price

        # SL/TPにかかった場合のUSDT差額を計算
        est_loss_usdt = (stop_loss - entry_price) * calc_amount
        est_profit_usdt = (take_profit - entry_price) * calc_amount
        
        # 表示用フォーマット
        est_pnl_line = f"  - **推定損益**: 🔴{format_usdt(est_loss_usdt)} / 🟢+{format_usdt(est_profit_usdt)} USDT\n"

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
            
            # 💡 注文タイプを自動で判断して表示 (V19.0.51ではIOC指値注文を想定)
            if '指値買い注文' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 指値買い注文が即時約定しなかったためキャンセルされました。"
            elif '成行買い注文' in error_message:
                 trade_status_line = f"❌ **自動売買 失敗**: 成行買い注文で約定が発生しませんでした。"
            else:
                 trade_status_line = f"❌ **自動売買 失敗**: {error_message}"
            
            # 💡 取引失敗詳細セクションの生成
            # SL/TP設定失敗後の強制クローズの結果を詳細に表示する
            close_status = trade_result.get('close_status')
            
            failure_section_lines = [f"  - ❌ {error_message}"]
            
            if close_status == 'ok':
                 close_amount = trade_result.get('closed_amount', 0.0)
                 close_message = f"✅ 不完全ポジションを即時クローズしました (数量: {close_amount:.4f})。"
                 failure_section_lines.append(f"  - {close_message}")
            elif close_status == 'error':
                 close_error = trade_result.get('close_error_message', '不明なエラー')
                 close_message = f"🚨 不完全ポジションの強制クローズに失敗しました: {close_error}"
                 failure_section_lines.append(f"  - {close_message}")
                 failure_section_lines.append(f"  - **🚨 ポジションが残っている可能性があります。手動で確認・決済してください。**")
            elif close_status == 'skipped':
                 failure_section_lines.append(f"  - ➖ ポジションは約定しなかった、または約定数量がゼロのためクローズはスキップされました。")
            
            failure_section = (
                f"\n💰 **取引失敗詳細**\n"
                f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                f"**取引失敗詳細**:\n"
                f"{'\n'.join(failure_section_lines)}\n"
            )
        
        elif trade_result.get('status') == 'ok':
            # ★ V19.0.45 修正: 注文タイプを明確化
            trade_status_line = "✅ **自動売買 成功**: 現物指値買い注文が即時約定しました。"
            filled_amount = trade_result.get('filled_amount', 0.0)
            filled_usdt = trade_result.get('filled_usdt', 0.0)
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 指値買い (IOC)</code>\n" # ★ IOCに変更
                f"  - **動的ロット**: {lot_info} (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
                f"  - **SL注文ID**: <code>{trade_result.get('sl_order_id', 'N/A')}</code>\n"
                f"  - **TP注文ID**: <code>{trade_result.get('tp_order_id', 'N/A')}</code>\n"
            )

    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        
        trade_status_line = f"🛑 **ポジション決済完了** ({exit_type_final})"
        pnl = trade_result.get('pnl_usdt', 0.0)
        pnl_percent = trade_result.get('pnl_percent', 0.0)
        
        # PnLの整形
        pnl_sign = "🟢" if pnl >= 0 else "🔴"
        pnl_text = f"{pnl_sign} <code>{format_usdt(pnl)}</code> USDT ({pnl_percent:+.2f}%)"
        
        trade_section = (
            f"💰 **取引決済結果**\n"
            f"  - **決済タイプ**: <code>{exit_type_final}</code>\n"
            f"  - **エントリー**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price_precision(trade_result.get('exit_price', 0.0))}</code>\n"
            f"  - **実現損益 (PnL)**: {pnl_text}\n"
            f"  - **総資産**: <code>{format_usdt(GLOBAL_TOTAL_EQUITY)}</code> USDT (更新)\n"
        )
    
    # メッセージ本体の組み立て
    message = (
        f"🔔 **{context}** - <b>{symbol}</b> ({timeframe})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **時刻**: {now_jst} (JST)\n"
        f"  - **スコア**: <code>{score*100:.2f} / 100</code> (閾値: {current_threshold*100:.2f})\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **エントリー (指値)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **リスクリワード (R:R)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(stop_loss)}</code> / <code>{format_price_precision(take_profit)}</code>\n"
        f"{est_pnl_line}" # ★ここで推定損益の行を追加
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"**{trade_status_line}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )

    # 💡 取引結果セクションを追加
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # 💡 失敗セクションがあれば追加
    if failure_section:
        message += failure_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"

    # 💡 スコア詳細ブレークダウンは、シグナル通知のコンテキストでのみ、成功/失敗に関わらず追加する
    if context == "取引シグナル":
        message += (
            f" \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f" <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
    
    # ★ v19.0.47 修正点: BOT_VERSION を使用
    message += (f"<i>Bot Ver: {BOT_VERSION} - Full Analysis & Async Refactoring</i>")
    
    return message

def format_hourly_report(signals: List[Dict], attempt_log: Dict[str, str], start_time: float, current_threshold: float, bot_version: str) -> str:
    """ 1時間ごとの最高・最低スコア銘柄の通知メッセージを作成する。 """
    global DEFAULT_SYMBOLS, BASE_SCORE
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    start_jst = datetime.fromtimestamp(start_time, JST).strftime("%H:%M:%S")
    
    # スコアでソート
    signals_sorted = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    # 💡【修正】分析対象とスキップの計算ロジック
    analyzed_count = len(signals)
    skipped_count = len(attempt_log) # attempt_log はスキップされたもののみ
    total_attempted = analyzed_count + skipped_count # 総試行数
    
    message = (
        f"📈 **【Hourly Report】** - {start_jst} から {now_jst} (JST)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **分析対象**: <code>{total_attempted}</code> 銘柄 / <code>{len(DEFAULT_SYMBOLS) * len(TARGET_TIMEFRAMES)}</code> タイムフレーム\n"
        f"  - **有効シグナル**: <code>{analyzed_count}</code> 件 (スコア > {BASE_SCORE*100:.0f})\n" # ベーススコア以上
        f"  - **スキップ**: <code>{skipped_count}</code> 銘柄 ({'、'.join(attempt_log.values()) if attempt_log else 'なし'})\n"
        f"  - **現在の取引閾値**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if not signals_sorted:
        message += f"\n➖ **有効なシグナルは見つかりませんでした**\n"
        message += (
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {bot_version} - Full Analysis & Async Refactoring</i>"
        )
        return message

    # ベストシグナル (最高スコア)
    best_signal = signals_sorted[0]
    message += (
        f"\n"
        f"🟢 **ベストスコア銘柄 (Top)**\n"
        f"  - **銘柄**: <b>{best_signal['symbol']}</b> ({best_signal['timeframe']})\n"
        f"  - **スコア**: <code>{best_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(best_signal['score'])}</code>\n"
        f"  - **指値 (Entry)**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"  - **SL/TP**: <code>{format_price_precision(best_signal['stop_loss'])}</code> / <code>{format_price_precision(best_signal['take_profit'])}</code>\n"
        f"\n"
    )
    
    # ワーストシグナル (最低スコア)
    # スコアがベーススコア(0.50)を超えているものだけを対象とする
    worst_signal_candidates = [s for s in signals_sorted if s['score'] > BASE_SCORE]
    if worst_signal_candidates:
        worst_signal = worst_signal_candidates[-1] # 最低スコアのシグナル
        message += (
            f"🔴 **ワーストスコア銘柄 (Low)**\n"
            f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
            f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
            f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
            f"  - **指値 (Entry)**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
            f"  - **SL/TP**: <code>{format_price_precision(worst_signal['stop_loss'])}</code> / <code>{format_price_precision(worst_signal['take_profit'])}</code>\n"
            f"\n"
        )
    else:
        message += f"\n➖ **ベーススコア以上のワーストシグナルは見つかりませんでした**\n"

    message += (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: {bot_version} - Full Analysis & Async Refactoring</i>"
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
        'symbol': signal.get('symbol'),
        'timeframe': signal.get('timeframe'),
        'score': signal.get('score'),
        'entry_price': signal.get('entry_price'),
        'stop_loss': signal.get('stop_loss'),
        'take_profit': signal.get('take_profit'),
        'rr_ratio': signal.get('rr_ratio'),
        'filled_usdt': signal.get('filled_usdt', signal.get('pnl_usdt')),
        'trade_result_status': signal.get('status', 'N/A'),
        'error_message': signal.get('error_message', 'N/A'),
        'pnl_percent': signal.get('pnl_percent', 'N/A'),
        # tech_dataは冗長になるため、ロギングからは除外
        #'tech_data': _to_json_compatible(signal.get('tech_data'))
    }
    
    # contextに応じてログレベルを調整
    if context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] < 0:
        # ロスカットまたはマイナス決済はWARN
        logging.warning(f"📉 {log_data['context']} - {log_data['symbol']} ({log_data['pnl_percent']:+.2f}%) - {log_data['trade_result_status']}")
    elif context == "ポジション決済":
        # プラス決済はINFO
        logging.info(f"📈 {log_data['context']} - {log_data['symbol']} ({log_data['pnl_percent']:+.2f}%) - {log_data['trade_result_status']}")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'ok':
        logging.info(f"🔔 {log_data['context']} - {log_data['symbol']} ({log_data['score']*100:.2f}) - {log_data['trade_result_status']}")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'error':
        logging.warning(f"❌ {log_data['context']} - {log_data['symbol']} ({log_data['score']*100:.2f}) - FAILED: {log_data['error_message']}")
    else:
        logging.debug(f"ℹ️ {log_data['context']} - {log_data['symbol']} - {log_data['trade_result_status']}")
        
    # JSONログファイルに追記
    try:
        with open('trade_log.jsonl', 'a') as f:
            f.write(json.dumps(_to_json_compatible(log_data)) + '\n')
    except Exception as e:
        logging.error(f"❌ ログファイルへの書き込み中にエラーが発生: {e}")

async def send_telegram_notification(message: str):
    """Telegramに通知を送信する非同期関数"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML' # HTMLタグを解釈
    }
    
    # asyncio.to_threadを使ってブロッキングなrequestsを非同期で実行
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info("✅ Telegram通知を送信しました。")
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ Telegram通知の送信に失敗 (HTTPエラー): {e} - レスポンス: {response.text if 'response' in locals() else 'N/A'}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知の送信に失敗 (リクエストエラー): {e}")

# ====================================================================================
# CCXT CLIENT & DATA FETCHING
# ====================================================================================

async def initialize_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    
    try:
        # クライアント名に応じてCCXTクライアントを動的に取得
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME)
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            # メキシコの場合、現物取引のカスタムパラメーターが必要になることがある
            # 'options': {
            #     'defaultType': 'spot', # 現物取引をデフォルトに設定
            # }
        })
        
        # 市場情報のロード（非同期）
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({EXCHANGE_CLIENT.name}) が正常に初期化されました。")
        
    except AttributeError:
        logging.error(f"❌ 取引所クライアント '{CCXT_CLIENT_NAME}' が見つかりません。")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)
        sys.exit(1)

async def fetch_account_status() -> Dict:
    """口座残高と総資産額を取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    
    try:
        # 1. 残高の取得
        balance_data = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高 (Free)
        total_usdt_balance = balance_data.get('free', {}).get('USDT', 0.0)
        
        # 2. 総資産額 (Equity) の計算 (USDT + その他の仮想通貨の時価評価額)
        total_equity = total_usdt_balance
        
        # USDT以外の保有資産をUSDT建てで評価
        for currency, amount in balance_data.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001:
                try:
                    symbol = f"{currency}/USDT"
                    # シンボルが取引所に存在するか確認
                    if symbol not in EXCHANGE_CLIENT.markets:
                         # ハイフンなしの形式も試す (例: BTCUSDT)
                        if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                             symbol = f"{currency}USDT"
                        else:
                             continue # スキップ
                    
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    usdt_value = amount * ticker['last']
                    if usdt_value >= 10: # 10 USDT未満の保有は無視
                         total_equity += usdt_value
                except Exception as e:
                    # エラーが発生した場合、その通貨の評価はスキップ
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")

        GLOBAL_TOTAL_EQUITY = total_equity # グローバル変数も更新
        logging.info(f"✅ 口座ステータス取得成功: Equity={format_usdt(GLOBAL_TOTAL_EQUITY)} USDT, Free USDT={format_usdt(total_usdt_balance)}")

        # USDT以外の保有資産の評価 (通知用)
        open_positions = []
        for currency, amount in balance_data.get('total', {}).items():
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
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

async def fetch_top_symbols() -> List[str]:
    """出来高TOPの銘柄リストを取得する"""
    global EXCHANGE_CLIENT, DEFAULT_SYMBOLS
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 市場更新がスキップされました。デフォルト銘柄リストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # 全てのティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建てのシンボルのみをフィルタリングし、出来高順にソート
        usdt_tickers = {
            symbol: data for symbol, data in tickers.items() 
            if '/USDT' in symbol and data and 'quoteVolume' in data and data['quoteVolume'] is not None
        }
        
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # TOP_SYMBOL_LIMIT までを抽出し、デフォルトリストと結合して重複を排除
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトリストとTOPリストを結合し、重複を排除
        combined_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高TOP銘柄リストを更新しました。合計 {len(combined_symbols)} 銘柄を監視します。")
        return combined_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得に失敗: {e}。デフォルト銘柄を使用します。")
        return DEFAULT_SYMBOLS.copy()


async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) などの市場環境データを取得する"""
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0 # 為替リスク要因は一旦未使用 (0.0)

    # 外部APIからFGIを取得するロジックをここに実装
    try:
        fgi_url = "https://api.alternative.me/fng/?limit=1"
        response = await asyncio.to_thread(requests.get, fgi_url, timeout=3)
        response.raise_for_status()
        data = response.json()
        
        if data and data['data']:
            fgi_value = int(data['data'][0]['value'])
            fgi_raw_value = f"{fgi_value} ({data['data'][0]['value_classification']})"
            
            # 0-100 のFGIを -0.05から+0.05のプロキシスコアに変換
            # FGI 50 (Neutral) -> 0.0
            # FGI 0 (Extreme Fear) -> -0.05
            # FGI 100 (Extreme Greed) -> +0.05
            # FGI_PROXY_BONUS_MAX = 0.05 を使用
            fgi_proxy = ((fgi_value - 50) / 50) * FGI_PROXY_BONUS_MAX
            
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value}, Proxy Score: {fgi_proxy:.3f}")
        
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生: {e}")
        # 失敗した場合は、全て0.0を返す
        pass
    
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus,
    }


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    # Simple Moving Averages (SMA)
    df['SMA_50'] = ta.sma(df['close'], length=50)
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) # 200
    
    # Relative Strength Index (RSI)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # Moving Average Convergence Divergence (MACD)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9, append=False)
    # MACDのデフォルトキーは 'MACD_12_26_9', 'MACDh_12_26_9', 'MACDs_12_26_9'
    if macd_data is not None and not macd_data.empty:
        df['MACD'] = macd_data.iloc[:, 0] # MACD line
        df['MACDh'] = macd_data.iloc[:, 1] # MACD histogram
        df['MACDs'] = macd_data.iloc[:, 2] # MACD signal line
    else:
        # MACD計算失敗時のフォールバック
        df['MACD'] = np.nan
        df['MACDh'] = np.nan
        df['MACDs'] = np.nan

    # Bollinger Bands (BBANDS) - 20期間, 2.0標準偏差
    bb_data = ta.bbands(df['close'], length=20, std=2.0, append=False)
    # ★★★ 修正箇所: KeyError: 'BBL_20_2.0' の修正 ★★★
    # pandas_taのバージョンアップにより、BBANDSのキーが 'BBL_20_2.0' -> 'BBL_20_2.0_2.0' に変更されました。
    if bb_data is not None and not bb_data.empty and 'BBL_20_2.0_2.0' in bb_data:
        df['BBL'] = bb_data['BBL_20_2.0_2.0'] # Lower Band
        df['BBM'] = bb_data['BBM_20_2.0_2.0'] # Middle Band (SMA)
        df['BBU'] = bb_data['BBU_20_2.0_2.0'] # Upper Band
    elif bb_data is not None and not bb_data.empty and 'BBL_20_2.0' in bb_data:
        df['BBL'] = bb_data['BBL_20_2.0']
        df['BBM'] = bb_data['BBM_20_2.0']
        df['BBU'] = bb_data['BBU_20_2.0']
    else:
        df['BBL'] = np.nan
        df['BBM'] = np.nan
        df['BBU'] = np.nan
    
    # Average True Range (ATR) - for stop loss calculation
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # OBVのSMA (シグナルライン)
    df['OBVsma'] = ta.sma(df['OBV'], length=20)
    
    return df


def analyze_ohlcv_and_score(
    symbol: str, 
    timeframe: str, 
    ohlcv: List[List], 
    market_ticker: Dict, 
    macro_context: Dict
) -> Optional[Dict]:
    """
    OHLCVデータと現在の市場状況を分析し、ロングシグナルスコアを計算する。
    Args:
        ohlcv: OHLCVデータのリスト (ccxt形式)
        market_ticker: 最新のティッカー情報 (ccxt形式)
        macro_context: FGIなどのマクロ環境情報
    Returns:
        シグナル情報を含む辞書、またはシグナルがない場合はNone
    """
    
    # 1. データフレームの準備
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
    
    # データの十分性のチェック
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    if len(df) < required_limit:
        logging.warning(f"⚠️ {symbol} ({timeframe}): データポイントが不足しています ({len(df)} < {required_limit})。スキップします。")
        return None
    
    # 2. インジケーターの計算
    try:
        df = calculate_indicators(df)
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): インジケーター計算エラー: {e}")
        return None

    # 最新のローソク足の情報を取得
    last_candle = df.iloc[-1]
    
    # 最後のローソク足のクローズ値がNoneまたはNaNの場合はスキップ
    if pd.isna(last_candle['close']):
        logging.debug(f"ℹ️ {symbol} ({timeframe}): 最新のクローズ値が利用できません。スキップします。")
        return None

    # 3. エントリー価格、SL/TPの計算
    current_price = market_ticker.get('last')
    
    # エントリー価格 (直近のローソク足の終値とする - 指値のトリガー価格)
    entry_price = last_candle['close'] 
    
    # トレンドフィルター: 
    # 長期SMAを上回っているか
    is_above_long_term_sma = last_candle['close'] > last_candle['SMA_200']
    
    # ATRが計算できていない場合はスキップ
    if pd.isna(last_candle['ATR']) or last_candle['ATR'] == 0:
        logging.warning(f"⚠️ {symbol} ({timeframe}): ATRが計算できません。スキップします。")
        return None

    # ストップロス (SL): ATRのn倍を使用
    # ATRの2.0倍を下回る価格をSLとする
    atr_multiplier = 2.0
    stop_loss = entry_price - (last_candle['ATR'] * atr_multiplier)

    # テイクプロフィット (TP): RR=1.5として、SL幅の1.5倍をTPとする
    rr_target = 1.5
    profit_target_distance = entry_price - stop_loss
    take_profit = entry_price + (profit_target_distance * rr_target)
    
    # SL/TPの妥当性チェック
    if stop_loss <= 0 or take_profit <= entry_price:
        # logging.warning(f"⚠️ {symbol} ({timeframe}): SL/TP計算が無効です (SL={stop_loss:.4f}, TP={take_profit:.4f})")
        return None

    # 4. スコアリング
    total_score = 0.0
    tech_data = {} # スコア詳細記録用

    # A. ベーススコア (50点)
    total_score += BASE_SCORE

    # B. 長期トレンド逆行ペナルティ (30点)
    # 価格がSMA200から大きく下回っている場合にペナルティ
    long_term_reversal_penalty_value = 0.0
    if not is_above_long_term_sma:
        # SMA200を下回っている場合、価格とSMA200の乖離率に応じてペナルティを適用
        deviation_ratio = (last_candle['SMA_200'] - last_candle['close']) / last_candle['SMA_200']
        # 乖離が大きくなるほどペナルティも大きくなる (最大でLONG_TERM_REVERSAL_PENALTY)
        # 乖離率が0.02 (2%)を超えると最大ペナルティ
        max_deviation = 0.02
        penalty_factor = min(deviation_ratio / max_deviation, 1.0)
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY * penalty_factor
        
        total_score -= long_term_reversal_penalty_value
        
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # C. 中期/長期トレンドアライメントボーナス (10点)
    # SMA50がSMA200の上にある (中期的な上昇トレンドの確認)
    trend_alignment_bonus_value = 0.0
    if last_candle['SMA_50'] > last_candle['SMA_200']:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        total_score += trend_alignment_bonus_value
        
    tech_data['trend_alignment_bonus_value'] = trend_alignment_bonus_value

    # D. 価格構造/ピボットボーナス (6点)
    # クローズ価格がSMA50を上回っていること (短期的な支持を確認)
    structural_pivot_bonus = 0.0
    if last_candle['close'] > last_candle['SMA_50']:
         structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
         total_score += structural_pivot_bonus
         
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # E. MACDペナルティ (25点)
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインを下回っている (デッドクロス/下落モメンタム)
    if last_candle['MACD'] < last_candle['MACDs']:
        macd_penalty_value = MACD_CROSS_PENALTY
        
        total_score -= macd_penalty_value
        
    tech_data['macd_penalty_value'] = macd_penalty_value

    # F. RSIモメンタムボーナス (10点)
    rsi_momentum_bonus_value = 0.0
    rsi_value = last_candle['RSI']
    
    # RSIがRSI_MOMENTUM_LOW (45)を下回り、上昇傾向にある場合をポジティブと評価 (買われすぎではない)
    if rsi_value < RSI_MOMENTUM_LOW:
        # RSIが低いほど、反発の余地が大きい
        # 0.0 から 1.0 の間で係数を計算 (RSI 0 -> 1.0, RSI 45 -> 0.0)
        rsi_factor = (RSI_MOMENTUM_LOW - rsi_value) / RSI_MOMENTUM_LOW
        
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * rsi_factor
        total_score += rsi_momentum_bonus_value

    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    tech_data['rsi_value'] = rsi_value

    # G. OBVモメンタムボーナス (5点)
    obv_momentum_bonus_value = 0.0
    # OBVがOBVのSMAを上回っている (買い圧力が優勢)
    if last_candle['OBV'] > last_candle['OBVsma']:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        total_score += obv_momentum_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # H. 出来高スパイクボーナス (7点)
    volume_increase_bonus_value = 0.0
    # 直近の出来高が過去20期間の出来高平均の2倍以上
    recent_volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].iloc[-20:-1].mean()
    
    if avg_volume > 0 and recent_volume > avg_volume * 2.0:
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        total_score += volume_increase_bonus_value
        
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 低ボラティリティペナルティ (変動)
    volatility_penalty_value = 0.0
    # BBANDSの幅が狭すぎる (低ボラティリティ、レンジ相場)
    bb_upper = last_candle['BBU']
    bb_lower = last_candle['BBL']
    
    if not pd.isna(bb_upper) and not pd.isna(bb_lower) and entry_price > 0:
        bb_width_ratio = (bb_upper - bb_lower) / entry_price
        if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD:
            # BB幅が閾値未満の場合、ペナルティを最大値として適用
            volatility_penalty_value = -MACD_CROSS_PENALTY * 0.5 # MACDペナルティの半分を適用
            total_score += volatility_penalty_value
            
    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # J. 流動性ボーナス (7点)
    # 板の厚みやスプレッドを評価する (今回はティッカーのボリュームと価格変動の少なさを基に簡易的に判断)
    liquidity_bonus_value = 0.0
    
    # 出来高と価格変動率の逆数を掛け合わせるなど
    # ここでは簡易的に、最新の出来高を過去平均と比較した相対的な「厚み」を使用
    if recent_volume > avg_volume:
        # 平均より出来高が多い場合は流動性があると見なす
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        
    total_score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # K. マクロ環境ボーナス/ペナルティ (5点)
    # FGIプロキシ + 為替ボーナス をそのままスコアに加算
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) + macro_context.get('forex_bonus', 0.0)
    # 最大ボーナス/ペナルティを FGI_PROXY_BONUS_MAX に制限
    sentiment_fgi_proxy_bonus = min(max(sentiment_fgi_proxy_bonus, -FGI_PROXY_BONUS_MAX), FGI_PROXY_BONUS_MAX)
    
    total_score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

    # スコアのクリッピング (0.0から1.0の間に収める)
    final_score = min(max(total_score, 0.0), 1.0)
    
    # 5. シグナルデータの組み立て
    signal = {
        'symbol': market_ticker['symbol'],
        'timeframe': timeframe,
        'score': final_score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_target,
        'current_price': current_price,
        'tech_data': tech_data,
        'last_log_time': time.time()
    }
    
    return signal

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """スコアと総資産に基づいて取引ロットサイズを動的に計算する"""
    global GLOBAL_TOTAL_EQUITY, DYNAMIC_LOT_MIN_PERCENT, DYNAMIC_LOT_MAX_PERCENT, DYNAMIC_LOT_SCORE_MAX
    
    # 1. 最小ロットサイズ (USDT)
    min_usdt_lot = BASE_TRADE_SIZE_USDT
    
    # 2. 総資産額の取得
    total_equity = account_status.get('total_equity', GLOBAL_TOTAL_EQUITY)
    
    # 総資産額が計算できていない、または少なすぎる場合は最小ロットを返す
    if total_equity < min_usdt_lot * (1 / DYNAMIC_LOT_MIN_PERCENT) or total_equity <= 0:
        return min_usdt_lot
    
    # 3. 動的ロットの計算範囲 (総資産のX%からY%)
    min_lot_from_equity = total_equity * DYNAMIC_LOT_MIN_PERCENT
    max_lot_from_equity = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 4. スコアによる重み付け (線形補間)
    # スコアが SIGNAL_THRESHOLD_NORMAL (0.84) 以下なら最小ロット。
    # DYNAMIC_LOT_SCORE_MAX (0.96) で最大ロット。
    base_threshold = SIGNAL_THRESHOLD_NORMAL
    max_score = DYNAMIC_LOT_SCORE_MAX
    
    if score <= base_threshold:
        weighted_lot_usdt = min_lot_from_equity
    elif score >= max_score:
        weighted_lot_usdt = max_lot_from_equity
    else:
        # スコアに基づいて線形補間
        # 補間範囲: base_threshold から max_score
        # 補間比率: (score - base_threshold) / (max_score - base_threshold)
        ratio = (score - base_threshold) / (max_score - base_threshold)
        weighted_lot_usdt = min_lot_from_equity + (max_lot_from_equity - min_lot_from_equity) * ratio
    
    # 5. 最小ロットと計算されたロットを比較し、大きい方を採用
    final_lot_usdt = max(weighted_lot_usdt, min_usdt_lot)
    
    # 6. 利用可能なUSDT残高を超えないように制限
    total_usdt_balance = account_status.get('total_usdt_balance', 0.0)
    final_lot_usdt = min(final_lot_usdt, total_usdt_balance)
    
    return final_lot_usdt

async def adjust_order_amount(symbol: str, lot_size_usdt: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小取引単位や精度に基づいて、注文数量 (Base Currency) を調整する。
    Args:
        symbol (str): 取引ペア (例: BTC/USDT)
        lot_size_usdt (float): USDT建ての目標ロットサイズ
        price (float): 注文価格 (指値価格)
    Returns:
        Tuple[float, float]: (調整後の数量, 調整後のUSDT価値)
    """
    global EXCHANGE_CLIENT
    
    if price <= 0 or lot_size_usdt <= 0 or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0
    
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.warning(f"⚠️ {symbol}: 市場情報が見つかりません。")
        return 0.0, 0.0
        
    # 目標の基軸通貨数量 (Base Currency Amount)
    target_amount = lot_size_usdt / price
    
    # 1. 数量の丸め（精度調整）
    amount_precision = market['precision'].get('amount', 8)
    # 数量を取引所の精度に丸める
    base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, target_amount)
    try:
        base_amount_rounded = float(base_amount_rounded)
    except ValueError:
        base_amount_rounded = 0.0

    # 2. 最小注文数量のチェック (最小注文数量 min_amount)
    min_amount = market['limits']['amount'].get('min', 0.0)
    if base_amount_rounded < min_amount:
        logging.warning(f"⚠️ {symbol}: 調整後数量 ({base_amount_rounded:.8f}) が最小取引数量 ({min_amount}) を満たしません。")
        return 0.0, 0.0

    final_usdt_amount = base_amount_rounded * price
    return base_amount_rounded, final_usdt_amount

# ====================================================================================
# TRADING EXECUTION
# ====================================================================================

async def place_sl_tp_orders(
    symbol: str, 
    filled_amount: float, 
    stop_loss: float, 
    take_profit: float
) -> Dict:
    """
    現物ポジションのストップロス (SL) とテイクプロフィット (TP) 注文を同時に設定する。
    Returns: {'status': 'ok', 'sl_order_id': '...', 'tp_order_id': '...'} または {'status': 'error', 'error_message': '...'}
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or filled_amount <= 0:
        return {'status': 'error', 'error_message': 'クライアント未準備または数量がゼロです。'}

    sl_order_id = None
    tp_order_id = None
    logging.info(f"⏳ SL/TP注文を設定中: {symbol} (Qty: {filled_amount:.4f}). SL={format_price_precision(stop_loss)}, TP={format_price_precision(take_profit)}")

    # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
    try:
        # 数量の丸め (ここでは価格はTP価格を使用)
        # filled_amount が既に丸められているため、ここでは adjust_order_amount は使わず、filled_amount をそのまま使用
        amount_to_sell = filled_amount
        
        # TP価格で指値売り
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=amount_to_sell,
            price=take_profit,
            params={ # TP注文を示すカスタムパラメータがあれば追加
                'clientOrderId': f'TP-{uuid.uuid4()}'
            }
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP注文成功: ID={tp_order_id}, Price={format_price_precision(take_profit)}")

    except Exception as e:
        # TP設定失敗は致命的ではないが、ログに記録
        logging.error(f"❌ TP注文失敗 ({symbol}): {e}")
        return {'status': 'error', 'error_message': f'TP注文失敗: {e}'}

    # 2. SL (ストップロス) ストップリミット売り注文の設定 (Stop Limit Sell)
    try:
        # Stop-Lossは取引所ごとに設定方法が異なるため、ccxtの unified stopLoss/takeProfit メソッドがあればそちらを使用
        # 現物取引では、通常はストップリミット注文（トリガー価格とリミット価格）を使用する
        # Stop Limit Order (Example for MEXC/Bybit Spot - requires custom params)
        
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if market and 'spot' in market['info'].get('permissions', []):
            
            # SL価格をトリガー価格とする (Trigger Price)
            stop_price = stop_loss
            # リミット価格 (Limit Price): トリガー価格より少し安く設定 (滑りを防ぐため)
            limit_price = stop_price * 0.999 # 例: -0.1%の滑り許容
            
            # 価格の精度調整
            limit_price_adj = EXCHANGE_CLIENT.price_to_precision(symbol, limit_price)
            stop_price_adj = EXCHANGE_CLIENT.price_to_precision(symbol, stop_price)
            
            sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='limit', # ccxtでは'stop limit'がない場合、'limit'にstopPriceなどのparamsを追加
                side='sell',
                amount=filled_amount,
                price=limit_price_adj, # Limit Price
                params={
                    'stopPrice': stop_price_adj, # Trigger Price
                    'triggerPrice': stop_price_adj, # 別名があれば
                    'type': 'STOP_LIMIT', # 取引所固有のタイプ指定があれば
                    'clientOrderId': f'SL-{uuid.uuid4()}'
                }
            )
            sl_order_id = sl_order['id']
            logging.info(f"✅ SL注文成功: ID={sl_order_id}, StopPrice={format_price_precision(stop_price)}")

        else:
            # 汎用的な方法が見つからない場合はエラー
            logging.error(f"❌ SL注文失敗: {EXCHANGE_CLIENT.name} の現物SL注文方法が未実装または不明です。")
            
            # TP注文をキャンセルしてからエラーを返す (不完全な状態を防ぐ)
            await cancel_sl_tp_orders(symbol, None, tp_order_id)
            
            return {'status': 'error', 'error_message': 'SL注文の設定が失敗しました。'}
            
    except Exception as e:
        logging.error(f"❌ SL注文失敗 ({symbol}): {e}")
        # TP注文をキャンセルしてからエラーを返す (不完全な状態を防ぐ)
        await cancel_sl_tp_orders(symbol, None, tp_order_id)
        return {'status': 'error', 'error_message': f'SL注文失敗: {e}'}

    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id
    }

async def cancel_sl_tp_orders(symbol: str, sl_order_id: Optional[str], tp_order_id: Optional[str]) -> bool:
    """SL/TPのオープン注文をキャンセルする"""
    global EXCHANGE_CLIENT
    
    success = True
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ クライアント未準備のためSL/TP注文キャンセルをスキップします。")
        return False
        
    if sl_order_id:
        try:
            await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
            logging.info(f"✅ SL注文キャンセル成功: {sl_order_id}")
        except ccxt.OrderNotFound:
            logging.info(f"ℹ️ SL注文 {sl_order_id} は既にキャンセル済みか約定済みです。")
        except Exception as e:
            logging.error(f"❌ SL注文キャンセル失敗 ({sl_order_id}): {e}")
            success = False

    if tp_order_id:
        try:
            await EXCHANGE_CLIENT.cancel_order(tp_order_id, symbol)
            logging.info(f"✅ TP注文キャンセル成功: {tp_order_id}")
        except ccxt.OrderNotFound:
            logging.info(f"ℹ️ TP注文 {tp_order_id} は既にキャンセル済みか約定済みです。")
        except Exception as e:
            logging.error(f"❌ TP注文キャンセル失敗 ({tp_order_id}): {e}")
            success = False
            
    return success

async def close_position_immediately(symbol: str, amount: float) -> Dict:
    """
    即時約定しない指値注文に失敗した場合など、不完全なポジションを成行でクローズする
    """
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or amount <= 0:
        return {'status': 'skipped', 'error_message': 'スキップ: クライアント未準備または数量がゼロです。'}

    logging.warning(f"🚨 不完全ポジションの強制クローズを試みます: {symbol} (数量: {amount:.4f})")
    
    try:
        # 数量の丸め（成行注文でも精度は重要）
        # 価格情報を取得し、amount_to_precisionで調整するために price_to_precision が必要だが、
        # ここでは簡易的に、現在の価格を取得して調整する
        try:
             ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
             current_price = ticker['last']
        except Exception:
             current_price = 1.0 # 取得失敗時は1.0として、USDT建てでの丸めを試みる
             
        # amount は基軸通貨の数量 (Base Amount)。これを取引所の精度に調整
        base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        base_amount_rounded = float(base_amount_rounded)
        amount = base_amount_rounded
        
        if amount <= 0:
             return {'status': 'skipped', 'error_message': '調整後の数量が最小要件を満たしません。'}


        # 成行売り注文
        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount
        )
        
        # 約定数量の確認
        closed_amount = close_order.get('filled', 0.0)
        
        if closed_amount > 0:
            logging.info(f"✅ 強制クローズ成功: {symbol} - {closed_amount:.4f} 数量を売却しました。")
            return {'status': 'ok', 'closed_amount': closed_amount}
        else:
            logging.error(f"❌ 強制クローズ失敗: 成行注文で約定が発生しませんでした。")
            return {'status': 'error', 'error_message': '成行売り注文が約定しませんでした。'}

    except Exception as e:
        logging.critical(f"❌ 強制クローズ中に致命的なエラーが発生 ({symbol}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'強制クローズ失敗（APIエラー）: {e}'}


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """
    シグナルに基づいて現物指値買い注文を発注し、SL/TP注文を設定する。
    Args:
        signal: シグナルデータ (symbol, entry_price, stop_loss, take_profitなどを含む)
        account_status: 口座残高情報
    Returns:
        取引結果辞書
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    
    # 1. 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    signal['lot_size_usdt'] = lot_size_usdt # シグナルデータにロットサイズを保存

    # 2. 注文数量の調整
    # 注文価格: entry_price (シグナルで決定した指値価格)
    base_amount_to_buy, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, entry_price)
    
    if base_amount_to_buy <= 0.0:
        return {'status': 'error', 'error_message': '調整後の数量が取引所の最小要件を満たしません。', 'close_status': 'skipped'}
    
    logging.info(f"⏳ 現物指値買い注文を発注中: {symbol} - {base_amount_to_buy:.4f} @ {format_price_precision(entry_price)}")

    # 3. 現物指値買い注文の発注 (IOC: Immediate-Or-Cancel)
    try:
        # IOC注文: 即時約定しなかった分はキャンセルされる
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount_to_buy,
            price=entry_price,
            params={
                # ここにIOCを指定するための取引所固有のパラメーターを追加
                'timeInForce': 'IOC', 
                'clientOrderId': f'ENTRY-{uuid.uuid4()}'
            }
        )
        
        # 4. 約定結果の確認
        filled_amount = order.get('filled', 0.0)
        filled_usdt = order.get('cost', 0.0)
        
        if filled_amount > 0:
            logging.info(f"✅ 指値買い注文 約定成功: {symbol} - {filled_amount:.4f} 数量 (Cost: {format_usdt(filled_usdt)} USDT)")

            # 5. SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol, 
                filled_amount, 
                signal['stop_loss'], 
                signal['take_profit']
            )

            if sl_tp_result['status'] == 'ok':
                # ポジションをグローバルリストに追加
                position_id = str(uuid.uuid4())
                OPEN_POSITIONS.append({
                    'id': position_id,
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'entry_time': time.time()
                })
                logging.info(f"✅ 新規ポジション ({symbol}) をトラッキングリストに追加しました。")

                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id']
                }
            else:
                # SL/TP設定失敗: 強制的にポジションをクローズ
                logging.critical(f"❌ SL/TP設定失敗 ({symbol})。約定済みポジションを強制クローズします。")
                close_result = await close_position_immediately(symbol, filled_amount)
                
                return {
                    'status': 'error',
                    'error_message': sl_tp_result.get('error_message', 'SL/TP設定中にエラーが発生'),
                    'close_status': close_result['status'],
                    'closed_amount': close_result.get('closed_amount', 0.0),
                    'close_error_message': close_result.get('error_message'),
                }
                
        else:
            # IOC注文で即時約定が発生しなかった場合
            error_message = f"指値買い注文が即時約定しませんでした (filled: 0.0)。注文はキャンセルされました。"
            logging.warning(f"⚠️ {symbol}: {error_message}")
            return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.InvalidOrder as e:
        error_message = f"無効な注文: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.NetworkError as e:
        error_message = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}

    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ 取引実行失敗 ({symbol}): {error_message}", exc_info=True)
        
        # 💡 CCXTエラーでも約定している可能性を考慮:
        # このエラーが返された時点で購入が成功し、SL/TP設定中にエラーが発生した可能性
        filled_amount_unknown = base_amount_to_buy # 注文した数量を暫定として強制クローズを試みる
        close_result = await close_position_immediately(symbol, filled_amount_unknown)
        
        return {
            'status': 'error',
            'error_message': f'取引所エラー（IOC/SL/TP設定失敗）: {e}',
            'close_status': close_result['status'],
            'closed_amount': close_result.get('closed_amount', 0.0),
            'close_error_message': close_result.get('error_message'),
        }

# ====================================================================================
# POSITION MANAGEMENT LOGIC
# ====================================================================================

async def check_order_status(order_id: str, symbol: str) -> Optional[Dict]:
    """注文IDとシンボルに基づいて注文ステータスを取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
    
    try:
        order = await EXCHANGE_CLIENT.fetch_order(order_id, symbol)
        return order
    except ccxt.OrderNotFound:
        # 注文が見つからない場合は、完全に約定したか、キャンセルされたと見なす
        return {'status': 'closed'}
    except Exception as e:
        logging.error(f"❌ 注文ステータス取得中にエラーが発生: {symbol}, ID={order_id}, Error: {e}")
        return None

async def cancel_and_re_place_sl_tp_orders(position: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    ポジションのSL/TP注文をキャンセルし、再設定する (v19.0.53で追加)
    """
    symbol = position['symbol']
    sl_order_id = position['sl_order_id']
    tp_order_id = position['tp_order_id']
    filled_amount = position['filled_amount']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    
    logging.warning(f"🔄 {symbol}: SL/TPの再設定を試行中。既存の注文 ({sl_order_id}, {tp_order_id}) をキャンセルします。")
    
    # 既存の注文をキャンセル
    await cancel_sl_tp_orders(symbol, sl_order_id, tp_order_id)
    
    # 新しい注文を再設定
    new_sl_tp_result = await place_sl_tp_orders(
        symbol,
        filled_amount,
        stop_loss,
        take_profit
    )
    
    if new_sl_tp_result['status'] == 'ok':
        logging.info(f"✅ {symbol}: SL/TP注文の再設定に成功しました。")
        # グローバルなOPEN_POSITIONSを更新
        position['sl_order_id'] = new_sl_tp_result['sl_order_id']
        position['tp_order_id'] = new_sl_tp_result['tp_order_id']
        return True, new_sl_tp_result
    else:
        logging.critical(f"❌ {symbol}: SL/TP注文の再設定に失敗しました。このポジションは手動での確認が必要です。")
        return False, new_sl_tp_result


async def open_order_management_loop():
    """オープン注文（ポジション）の状態を監視し、決済されたものを処理する"""
    global OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    if not OPEN_POSITIONS:
        return

    logging.debug(f"ℹ️ オープン注文監視ループ開始。監視中のポジション: {len(OPEN_POSITIONS)}")
    positions_to_remove_ids = []

    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_order_id = position['sl_order_id']
        tp_order_id = position['tp_order_id']
        is_closed = False
        closed_result = None
        exit_type = None
        
        # 1. SL/TP注文のステータスチェック
        sl_status = await check_order_status(sl_order_id, symbol)
        tp_status = await check_order_status(tp_order_id, symbol)

        # 💡 【V19.0.53 追加ロジック】 SL/TP注文の片方または両方が欠けている場合の再設定
        sl_open = sl_status and sl_status['status'] in ('open', 'limit')
        tp_open = tp_status and tp_status['status'] in ('open', 'limit')

        if not sl_open or not tp_open:
            if sl_status and sl_status['status'] == 'closed' and tp_status and tp_status['status'] == 'closed':
                 # 両方とも 'closed' の場合、手動決済された可能性が高いと見なす
                 exit_type = '手動決済'
                 is_closed = True
                 # SL/TPのどちらが先に約定したかは分からないため、ここでは手動決済とする
                 logging.info(f"🛑 {symbol}: 両方のSL/TP注文がCLOSEDステータス。手動決済された可能性があります。")
            elif sl_status and sl_status['status'] == 'closed':
                 # SLが約定
                 exit_type = 'SL約定'
                 # 残っているTP注文をキャンセル
                 await cancel_sl_tp_orders(symbol, None, tp_order_id)
                 is_closed = True
                 logging.info(f"🛑 {symbol}: SL約定を確認しました。TP注文をキャンセルします。")
            elif tp_status and tp_status['status'] == 'closed':
                 # TPが約定
                 exit_type = 'TP約定'
                 # 残っているSL注文をキャンセル
                 await cancel_sl_tp_orders(symbol, sl_order_id, None)
                 is_closed = True
                 logging.info(f"🛑 {symbol}: TP約定を確認しました。SL注文をキャンセルします。")
            elif not sl_open or not tp_open:
                 # どちらか一方 (または両方) の注文が 'open' ではないが 'closed' でも 'cancelled' でもない場合、
                 # または、fetch_orderの結果がNoneなどで注文の状態が不明な場合
                 logging.warning(f"⚠️ {symbol}: SL/TP注文のいずれか (SL: {sl_open}, TP: {tp_open}) の状態が不明または欠落しています。再設定を試みます。")
                 
                 # ⚠️ 再設定ロジックを呼び出す
                 success, re_place_result = await cancel_and_re_place_sl_tp_orders(position)
                 if not success:
                     # 再設定失敗は、手動での確認を促すログとして残す
                     logging.critical(f"🚨 {symbol}: SL/TP注文の再設定に失敗しました。手動での確認が必要です。")
        
        elif sl_status and sl_status['status'] == 'canceled':
             # 注文がキャンセルされたままになっている場合 (手動キャンセルまたはボットのバグ)
             logging.warning(f"⚠️ {symbol}: SL注文がキャンセルされています。再設定を試みます。")
             await cancel_and_re_place_sl_tp_orders(position)

        elif tp_status and tp_status['status'] == 'canceled':
             # 注文がキャンセルされたままになっている場合 (手動キャンセルまたはボットのバグ)
             logging.warning(f"⚠️ {symbol}: TP注文がキャンセルされています。再設定を試みます。")
             await cancel_and_re_place_sl_tp_orders(position)

        else:
             # 両方の注文がオープン中
             logging.debug(f"ℹ️ {symbol} は引き続きオープン中 (SL: {sl_open}, TP: {tp_open})")
             pass
        
        
        if is_closed:
            positions_to_remove_ids.append(position['id'])
            
            # 約定価格は履歴から取得が必要だが、ここでは簡略化のため0.0とする
            # PnLの計算は、最新の市場価格を使用して行う（または取引所APIから最終約定価格を取得）
            try:
                # 最終的なPnL計算のため、最新価格を取得
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                last_price = ticker['last']
                
                # 簡略化したPnL計算
                entry_price = position['entry_price']
                exit_price = last_price # 暫定的に最新価格を決済価格とする
                
                # 決済価格を、SL/TPの約定価格 (sl_status/tp_status の average) から取得できればより正確
                if exit_type == 'SL約定' and sl_status and sl_status.get('average'):
                    exit_price = sl_status['average']
                elif exit_type == 'TP約定' and tp_status and tp_status.get('average'):
                    exit_price = tp_status['average']
                # ユーザー手動決済の場合は、最新価格を使用するしかない
                
                pnl_usdt = (exit_price - entry_price) * position['filled_amount']
                pnl_percent = (pnl_usdt / position['filled_usdt']) * 100 if position['filled_usdt'] > 0 else 0.0
                
                # 総資産額の再取得
                account_status_after_close = await fetch_account_status()
                current_total_equity = account_status_after_close.get('total_equity', GLOBAL_TOTAL_EQUITY)
                GLOBAL_TOTAL_EQUITY = current_total_equity # グローバル更新
                
                closed_result = {
                    'symbol': position['symbol'],
                    'entry_price': position['entry_price'],
                    'exit_price': exit_price,
                    'pnl_usdt': pnl_usdt,
                    'pnl_percent': pnl_percent,
                    'exit_type': exit_type,
                    'status': 'ok',
                    'total_equity_after': current_total_equity
                }
                
                # 決済通知
                # 決済時には timeframe 情報がないため、'1h'を仮で設定
                signal_for_log = {'symbol': symbol, 'timeframe': '1h'}
                notification_message = format_telegram_message(signal_for_log, "ポジション決済", SIGNAL_THRESHOLD_NORMAL, closed_result)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "ポジション決済")

            except Exception as e:
                logging.error(f"❌ 決済処理後のPnL計算/通知中にエラーが発生 ({symbol}): {e}")
        
    # 決済完了したポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]
    logging.debug(f"ℹ️ オープン注文監視ループ完了。現在のポジション数: {len(OPEN_POSITIONS)}")


async def fetch_ohlcv_and_analyze(
    symbol: str, 
    timeframe: str, 
    market_ticker: Dict, 
    macro_context: Dict
) -> Optional[Dict]:
    """
    特定の銘柄とタイムフレームのOHLCVを取得し、分析・スコアリングを行う。
    エラーが発生した場合はNoneを返す。
    """
    global EXCHANGE_CLIENT
    
    try:
        # OHLCVの取得
        required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        # limit は必要なデータポイント数より少し多めに設定 (計算に必要な期間分をカバーするため)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=required_limit + 200) 
        
        if len(ohlcv) < required_limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが不足しています ({len(ohlcv)} < {required_limit})。")
            HOURLY_ATTEMPT_LOG[symbol] = "データ不足"
            return None

        # 分析とスコアリング
        signal = analyze_ohlcv_and_score(symbol, timeframe, ohlcv, market_ticker, macro_context)
        return signal

    except Exception as e:
        # このエラーは analyze_symbol でキャッチされる
        raise Exception(f"OHLCV Fetch/Indicator Calc Error for {symbol} ({tf}): {e}")

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def analyze_symbol_wrapper(symbol: str, market_tickers: Dict, macro_context: Dict) -> List[Dict]:
    """
    単一の銘柄に対して、全てのタイムフレームで分析を実行し、有効なシグナルをリストで返す。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS, HOURLY_ATTEMPT_LOG
    
    signals: List[Dict] = []
    
    # クールダウンチェック
    if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
        logging.debug(f"ℹ️ {symbol}: クールダウン期間中のためスキップします。")
        HOURLY_ATTEMPT_LOG[symbol] = "クールダウン"
        return []
    
    # 既にポジションを持っている銘柄はスキップ (現物なので両建て不可)
    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
        logging.debug(f"ℹ️ {symbol}: ポジション保有中のためスキップします。")
        HOURLY_ATTEMPT_LOG[symbol] = "ポジション保有"
        return []

    # ティッカー情報のチェック
    market_ticker = market_tickers.get(symbol)
    if not market_ticker:
        logging.warning(f"⚠️ {symbol}: ティッカー情報が見つかりません。スキップします。")
        HOURLY_ATTEMPT_LOG[symbol] = "ティッカーなし"
        return []

    # 複数タイムフレームでの分析を並列実行
    analysis_tasks = [
        fetch_ohlcv_and_analyze(symbol, tf, market_ticker, macro_context)
        for tf in TARGET_TIMEFRAMES
    ]
    
    results = await asyncio.gather(*analysis_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            # 個別の分析タスクでエラーが発生した場合
            logging.error(f"❌ {symbol}: 分析中にエラーが発生: {result}")
            HOURLY_ATTEMPT_LOG[symbol] = "分析エラー"
            continue
        
        if result is not None:
            # 有効なシグナルが見つかった場合
            signals.append(result)

    # 失敗しなかったがシグナルも見つからなかった場合
    if not signals and symbol not in HOURLY_ATTEMPT_LOG:
         HOURLY_ATTEMPT_LOG[symbol] = "シグナルなし"

    return signals

async def run_all_analysis(market_tickers: Dict, macro_context: Dict) -> List[Dict]:
    """全ての監視銘柄に対して分析を並列実行する"""
    global CURRENT_MONITOR_SYMBOLS
    
    logging.info(f"⏳ 全 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄の分析を並列実行します...")
    
    # 並列処理のタスクリスト
    analysis_tasks = [
        analyze_symbol_wrapper(symbol, market_tickers, macro_context)
        for symbol in CURRENT_MONITOR_SYMBOLS
    ]
    
    # 全てのタスクを実行
    results = await asyncio.gather(*analysis_tasks)
    
    # 結果をフラット化
    all_signals = [signal for sublist in results for signal in sublist]
    
    # スコアの高い順にソート
    all_signals_sorted = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    
    logging.info(f"✅ 全分析完了。有効なシグナル数: {len(all_signals_sorted)}")
    return all_signals_sorted


async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() # FGIの値をスコアリングに反映する準備
    macro_influence_score = (GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) + GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0))
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 2. 口座ステータスを取得し、新規取引の可否をチェック
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("🚨 口座ステータスの取得に失敗しました。取引をスキップします。")
        # 初回起動完了通知をスキップさせないため、一旦処理を続行させる

    # 3. 出来高上位銘柄を更新 (1時間ごと)
    if time.time() - LAST_SUCCESS_TIME > 60 * 60:
        CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
        LAST_SUCCESS_TIME = time.time()
        
    # 4. 全ての監視銘柄の最新ティッカーを取得 (並行処理の効率化のため)
    market_tickers = {}
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols=CURRENT_MONITOR_SYMBOLS)
        market_tickers = {k: v for k, v in tickers.items() if v}
    except Exception as e:
        logging.error(f"❌ ティッカー情報の取得に失敗: {e}")

    # 5. 全ての監視銘柄の分析を実行
    HOURLY_ATTEMPT_LOG = {} # 1時間ごとの試行ログをリセット
    new_signals = await run_all_analysis(market_tickers, GLOBAL_MACRO_CONTEXT)
    LAST_ANALYSIS_SIGNALS = new_signals # グローバルに保存
    
    # 6. Hourly Report用のシグナルログを更新
    # 既存のシグナルログに新しいシグナルを追加 (重複チェックなしで全て追加)
    HOURLY_SIGNAL_LOG.extend(new_signals) 
    
    # 7. Hourly Reportの通知 (1時間ごと)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("⏳ Hourly Reportの作成と送信を開始します。")
        
        # ログをスコアでソートし、上位/下位のシグナルを通知
        notification_message = format_hourly_report(
            HOURLY_SIGNAL_LOG, 
            HOURLY_ATTEMPT_LOG, 
            LAST_HOURLY_NOTIFICATION_TIME, # 前回の通知時刻をレポートの開始時刻とする
            current_threshold, 
            BOT_VERSION
        )
        await send_telegram_notification(notification_message)
        
        # ログと時刻をリセット
        HOURLY_SIGNAL_LOG = []
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
    # 8. 最適なシグナルを選択し、取引を実行
    
    # スコアが高く、かつクールダウンが切れている最初のシグナル
    best_signal = None
    
    for signal in new_signals:
        if time.time() - LAST_SIGNAL_TIME.get(signal['symbol'], 0) > TRADE_SIGNAL_COOLDOWN:
            best_signal = signal
            break
    
    trade_result = None
    
    if best_signal:
        logging.info(f"💡 最適なシグナル: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score']*100:.2f}")

        # 9. 閾値チェック
        if best_signal['score'] >= current_threshold:
            
            # 10. 取引実行チェック (残高、テストモード)
            if not TEST_MODE and account_status.get('total_usdt_balance', 0.0) >= MIN_USDT_BALANCE_FOR_TRADE:
                # 実行
                trade_result = await execute_trade(best_signal, account_status)
                
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time() # 成功時にクールダウン開始
                    log_signal(best_signal, "取引シグナル")
                else:
                    log_signal(best_signal, "取引シグナル (失敗)")
                
            elif TEST_MODE:
                trade_result = {'status': 'info', 'error_message': 'テストモードのため取引をスキップ', 'close_status': 'skipped'}
                logging.info(f"ℹ️ {best_signal['symbol']} (Score: {best_signal['score']*100:.2f}): TEST_MODEのため取引をスキップしました。")
                log_signal(best_signal, "取引シグナル (テストモード)")
                pass
            
            else:
                error_message = f"残高不足 (現在: {format_usdt(account_status['total_usdt_balance'])} USDT)。新規取引に必要な額: {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT。"
                trade_result = {'status': 'error', 'error_message': error_message, 'close_status': 'skipped'}
                logging.warning(f"⚠️ {best_signal['symbol']} 取引スキップ: {error_message}")
                log_signal(best_signal, "取引シグナル (残高不足)")
                
        else:
            logging.info(f"ℹ️ {best_signal['symbol']} は閾値 {current_threshold*100:.2f} を満たしていません。取引をスキップします。")
            # 閾値を満たさないシグナルもログには記録する
            log_signal(best_signal, "取引シグナル (閾値未満/テストモード)")
            
    # 11. Telegram通知
    if trade_result and trade_result.get('status') in ('ok', 'error'):
        # 取引成功または取引失敗の場合に通知を送信
        notification_message = format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
    elif best_signal and best_signal['score'] >= current_threshold and trade_result is None:
        # 閾値は超えたがシステム的に取引が実行されなかった場合（例：クールダウンではないが、処理に失敗したなど）
        logging.error(f"❌ {best_signal['symbol']} の取引処理に不明なエラーが発生しました。")


    # 12. 初回起動完了通知 (一度だけ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動通知
        status_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
        await send_telegram_notification(status_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        logging.info("✅ 初回メインループ処理と起動通知を完了しました。")

    end_time = time.time()
    logging.info(f"--- 💡 BOT LOOP END. Duration: {end_time - start_time:.2f}s ---")


async def main_async_startup():
    """非同期処理の開始とメインループのスケジューリング"""
    await initialize_client()
    
    # ポジション監視ループ (メインループとは独立して実行)
    asyncio.create_task(periodic_task(open_order_management_loop, MONITOR_INTERVAL))
    
    # メインBOTループ (1分ごと)
    await periodic_task(main_bot_loop, LOOP_INTERVAL)
    
async def periodic_task(func: Callable, interval: int):
    """指定された間隔で関数を実行し続けるタスクを管理する"""
    # 最初の実行を待たずに即時実行
    if func == main_bot_loop:
        await func()
    
    while True:
        try:
            # 実行時間計測と調整
            start = time.time()
            if func != main_bot_loop: # メインループは初回実行済み
                await func()
            
            elapsed = time.time() - start
            wait_time = max(0, interval - elapsed)
            
            await asyncio.sleep(wait_time)
            
        except asyncio.CancelledError:
            logging.info(f"ℹ️ {func.__name__} の定期実行タスクがキャンセルされました。")
            break
        except Exception as e:
            logging.critical(f"🚨 {func.__name__} の実行中に致命的なエラーが発生: {e}", exc_info=True)
            
            # メインループでエラーが発生した場合、Telegramに通知
            if func == main_bot_loop:
                 error_message = f"🚨 **致命的なエラー発生**\n\nメインBOTループの実行中にエラーが発生しました。\n\n**エラー:** <code>{str(e)[:500]}...</code>\n\n**BOTバージョン**: <code>{BOT_VERSION}</code>"
                 try:
                      await send_telegram_notification(error_message)
                      logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
                 except Exception as notify_e:
                      logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

            # 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI(
    title="Apex BOT Status API",
    description="Apex BOTのリアルタイムステータスとログを提供するAPI",
    version=BOT_VERSION
)

@app.get("/")
def read_root():
    """ルートエンドポイント"""
    return {"message": f"Apex BOT is running. Version: {BOT_VERSION}"}

@app.get("/status")
async def get_status():
    """現在のボットの状態を返す"""
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    status_data = {
        "version": BOT_VERSION,
        "is_client_ready": IS_CLIENT_READY,
        "test_mode": TEST_MODE,
        "loop_interval_sec": LOOP_INTERVAL,
        "total_equity_usdt": GLOBAL_TOTAL_EQUITY,
        "macro_context": GLOBAL_MACRO_CONTEXT,
        "current_signal_threshold": current_threshold,
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "open_positions_count": len(OPEN_POSITIONS),
        "open_positions": OPEN_POSITIONS,
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "last_signals_top_3": sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x['score'], reverse=True)[:3],
        "hourly_report_log_count": len(HOURLY_SIGNAL_LOG),
        "hourly_attempt_log": HOURLY_ATTEMPT_LOG,
        "is_first_main_loop_completed": IS_FIRST_MAIN_LOOP_COMPLETED,
        "last_successful_update": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A"
    }
    
    return JSONResponse(content=status_data)


# ====================================================================================
# MAIN EXECUTION
# ====================================================================================

if __name__ == "__main__":
    
    # 1. バックグラウンドでメインBOTループを実行するタスクを作成
    bot_task = asyncio.ensure_future(main_async_startup())
    
    # 2. FastAPIサーバーを起動
    # Uvicornの起動時に、非同期タスクをループに追加するためにカスタムのStartupイベントを使用
    
    # NOTE: uvicorn.run はブロッキングなので、asyncio.run()内で実行し、
    # Botのタスクと協調動作させる必要がある。
    # ここでは簡易的に、FastAPIのイベントハンドラでTaskを管理する標準的な方法を使用する。
    
    @app.on_event("startup")
    async def startup_event():
        # Botの非同期タスクが既に外部で確保されているため、ここでは何もしない。
        # uvicornが実行されるプロセスで、asyncio.run() 相当の動作をさせるため、
        # uvicorn.run(app) を直接呼び出す。
        pass
    
    # FastAPIの起動と、Botのasyncioループを同じプロセスで実行するためのUvicorn設定
    try:
        # 💡 main_async_startup() のタスクが既に作成されているため、Uvicornのライフサイクルに任せる
        # メインループを asyncio.run(main_async_startup()) で実行し、FastAPIを別プロセスで起動する代わりに、
        # Uvicornの 'factory pattern' または 'app.run()' の仕組みを活用する。
        # 既存のコードは、Uvicornのカスタム実行を想定している可能性が高い。
        # 以下の uvicorn.run は、メインBotタスクの非同期性を維持する設定で実行する。
        logging.info("🚀 Uvicorn/FastAPIサーバーを起動中...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except KeyboardInterrupt:
        logging.info("🧹 サーバーとボットをシャットダウンします。")
        bot_task.cancel()
    except Exception as e:
        logging.critical(f"🔥 Uvicorn/FastAPI実行中に致命的なエラーが発生: {e}", exc_info=True)
        bot_task.cancel()
