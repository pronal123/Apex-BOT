# ====================================================================================
# Apex BOT v19.0.53 (Patched) - FEATURE: Periodic SL/TP Re-Placing for Unmanaged Orders
#
# 改良・修正点:
# 1. 【SL/TP再設定】open_order_management_loop関数内に、SLまたはTPの注文が片方または両方欠けている場合に、
#    残っている注文をキャンセルし、SL/TP注文を再設定するロジックを追加。
# 2. 【IOC失敗診断維持】v19.0.52で追加したIOC失敗時診断ログを維持。
# 3. 【レポート表示修正】Hourly Reportの分析対象数計算ロジックを修正 (v19.0.53-p1)
# 4. 【通知強化】取引シグナル通知に推定損益(USDT)を表示する機能を追加 (v19.0.53-p1)
# 5. 【修正】CCXT MEXC fetch_balance() NotSupportedエラー対策
# 6. 【修正】Yfinance Too Many Requests (レート制限) 対策として、60分クールダウンのキャッシュロジックを追加。
# 7. 【修正】fetch_top_symbols() の NoneType エラー対策。
# 8. 【★★★ 新規修正 (Fix 4) ★★★】CCXT初期化時の `RuntimeError: Event loop is closed` 対策として、
#    `initialize_ccxt_client` を非同期化し、`await` で `load_markets()` を呼び出すように変更。
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
# Yfinance レート制限エラーログに対応するため、ライブラリをインポート
try:
    import yfinance as yf
except ImportError:
    logging.warning("⚠️ yfinanceライブラリが見つかりません。為替ボーナス機能は無効になります。")
    yf = None

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

# ★ 新規追加: Yfinance レート制限対策用 (Fix 2)
LAST_FOREX_FETCH_TIME: float = 0.0 # 最終的な為替データ取得成功時刻
FOREX_COOLDOWN: int = 60 * 60      # 為替データを再取得するクールダウン (60分)
FOREX_CACHE: Dict = {'forex_bonus': 0.0} # 為替データキャッシュ

# ★ 新規追加: ボットのバージョン (v19.0.53-p2: Event Loop Bugfix)
BOT_VERSION = "v19.0.53-p2"

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
    global DEFAULT_SYMBOLS
    
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
        f"  - **分析対象**: <code>{total_attempted}</code> 銘柄 (分析: <code>{analyzed_count}</code> / スキップ: <code>{skipped_count}</code>)\n"
        f"  - **現在の閾値**: <code>{current_threshold*100:.2f} / 100</code>\n"
        f"  - **総資産額 (Equity)**: <code>{format_usdt(GLOBAL_TOTAL_EQUITY)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # --- TOP 3 シグナル ---
    message += f"\n🥇 **トップ・シグナル (Top {min(3, analyzed_count)})**\n"
    if not signals_sorted:
        message += f"  - 該当なし\n"
    else:
        for i, signal in enumerate(signals_sorted[:3]):
            entry_price_disp = format_price_precision(signal['entry_price'])
            sl_disp = format_price_precision(signal['stop_loss'])
            tp_disp = format_price_precision(signal['take_profit'])
            
            message += (
                f"  - <b>{i+1}. {signal['symbol']}</b> ({signal['timeframe']})\n"
                f"    - **Score**: <code>{signal['score']*100:.2f}</code> ({get_estimated_win_rate(signal['score'])})\n"
                f"    - **Entry**: <code>{entry_price_disp}</code> (SL: {sl_disp} / TP: {tp_disp})\n"
            )
            
    # --- ワースト・シグナル ---
    message += f"\n💀 **ワースト・シグナル (Worst 1)**\n"
    if analyzed_count < 2:
        message += f"  - 該当なし\n"
    else:
        worst_signal = signals_sorted[-1]
        entry_price_disp = format_price_precision(worst_signal['entry_price'])
        sl_disp = format_price_precision(worst_signal['stop_loss'])
        tp_disp = format_price_precision(worst_signal['take_profit'])
        
        message += (
            f"  - <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
            f"    - **Score**: <code>{worst_signal['score']*100:.2f}</code> ({get_estimated_win_rate(worst_signal['score'])})\n"
            f"    - **Entry**: <code>{entry_price_disp}</code> (SL: {sl_disp} / TP: {tp_disp})\n"
        )
        
    # --- スキップされた銘柄 ---
    message += f"\n⚠️ **スキップされた銘柄 ({skipped_count})**\n"
    if skipped_count == 0:
        message += f"  - なし\n"
    else:
        # スキップ理由を集計し、多い順に表示
        reason_counts = {}
        for reason in attempt_log.values():
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            
        sorted_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
        
        for reason, count in sorted_reasons[:3]:
             message += f"  - <code>{count}</code> 銘柄: {reason}\n"
        if len(sorted_reasons) > 3:
             message += f"  - ...他 {len(sorted_reasons) - 3} 種類\n"
            
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
        logging.warning(f"📉 SIGNAL_LOG ({context}): {log_data['symbol']} - PnL: {log_data['pnl_percent']:+.2f}%")
    elif context == "ポジション決済" and log_data['pnl_percent'] is not None and log_data['pnl_percent'] >= 0:
        logging.info(f"📈 SIGNAL_LOG ({context}): {log_data['symbol']} - PnL: {log_data['pnl_percent']:+.2f}%")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'ok':
         logging.info(f"✅ SIGNAL_LOG ({context}): {log_data['symbol']} - Score: {log_data['score']*100:.2f}")
    elif context == "取引シグナル" and log_data['trade_result_status'] == 'error':
         logging.error(f"❌ SIGNAL_LOG ({context}): {log_data['symbol']} - Error: {log_data['error_message']}")
    else:
        logging.debug(f"ℹ️ SIGNAL_LOG ({context}): {log_data['symbol']}")

    # JSONファイルへの書き込み（ログファイルが大きくなるため、ここでは標準出力のみとする）
    # print(json.dumps(log_data, ensure_ascii=False))

# ★ 修正点 (Fix 4): initialize_ccxt_client を async に変更
async def initialize_ccxt_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    logging.info(f"✅ CCXTクライアントをプライベート操作可能として初期化します。")
    
    # 選択された取引所のクライアントクラスを取得
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower(), None)
    
    if exchange_class is None:
        logging.critical(f"🚨 サポートされていない取引所: {CCXT_CLIENT_NAME}")
        sys.exit(1)
        
    try:
        # クライアントのインスタンス化 (現物取引を想定し、期日やレバレッジ設定はなし)
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True, # レート制限対策を有効化
            'timeout': 30000, # タイムアウトを30秒に設定
            'options': {
                'defaultType': 'spot', # 現物取引をデフォルトとする
            }
        })
        
        logging.info(f"✅ CCXTクライアント ({EXCHANGE_CLIENT.name}) の初期化に成功しました。")
        
        # ロードマーケットを実行してマーケットデータをキャッシュ (asyncio.run()を削除し、awaitに変更)
        await EXCHANGE_CLIENT.load_markets() 
        
        IS_CLIENT_READY = True
        
    except Exception as e:
        logging.critical(f"🚨 CCXTクライアントの初期化に失敗: {e}", exc_info=True)
        # 環境変数エラーなど致命的なエラーの場合は終了させる
        sys.exit(1)


async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML', # HTML形式でメッセージをパース
        'disable_web_page_preview': True, # プレビューを無効化
    }

    try:
        # requestsは同期的なので、asyncio.to_threadを使用して別スレッドで実行
        await asyncio.to_thread(requests.post, url, data=payload, timeout=10)
        logging.debug("✅ Telegram通知を送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗: {e}")

async def adjust_order_amount(symbol: str, target_usdt_amount: float, price: float) -> Tuple[float, float]:
    """
    取引所の最小数量、最小ロット、精度のルールに基づいて注文数量を調整し、
    最終的なUSDT投入額を計算する。
    """
    global EXCHANGE_CLIENT
    
    # 1. 注文数量の計算
    base_currency = symbol.split('/')[0]
    
    if price <= 0 or target_usdt_amount <= 0:
        logging.warning(f"⚠️ {symbol}: 注文数量の計算が無効な値です (Price: {price}, USDT: {target_usdt_amount})")
        return 0.0, 0.0
        
    base_amount = target_usdt_amount / price
    
    # 2. 取引所ルールの取得と適用
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        logging.error(f"❌ {symbol} のマーケットデータが見つかりません。デフォルト精度を使用します。")
        # 安全のために、デフォルトの丸め精度を設定
        amount_precision = 4 
        min_amount = 0.0001
        min_cost = 10.0 # 最小ロット 10 USDT
    else:
        # 数量(amount)の精度 (decimals)
        amount_precision = market['precision'].get('amount', 4)
        # 最小数量
        min_amount = market['limits'].get('amount', {}).get('min', 0.0001)
        # 最小ロット (USDTコスト)
        min_cost = market['limits'].get('cost', {}).get('min', 10.0)

    # 3. 最小ロットチェック (USDT換算)
    # 最小ロットを下回る場合は調整
    if target_usdt_amount < min_cost:
        logging.warning(f"⚠️ {symbol}: ロットサイズが最小ロット ({min_cost:.2f} USDT) 未満です。最小ロットに切り上げます。")
        target_usdt_amount = min_cost
        base_amount = target_usdt_amount / price
    
    # 4. 数量の最小値チェック
    if base_amount < min_amount:
        logging.error(f"❌ {symbol}: 計算数量 ({base_amount:.8f} {base_currency}) が最小数量 ({min_amount:.8f} {base_currency}) 未満です。スキップします。")
        return 0.0, 0.0
    
    # 5. 数量の丸め (CCXTの safe_amount を使用することが最も安全)
    # 精度が None の場合を考慮
    if amount_precision is None:
        amount_precision = 4 # デフォルトで小数第4位
        
    base_amount_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)

    try:
        base_amount_rounded = float(base_amount_rounded)
    except:
        logging.error(f"❌ CCXTのamount_to_precision結果が不正です: {base_amount_rounded}")
        return 0.0, 0.0
    
    # 最終的な投入額を計算
    final_usdt_amount = base_amount_rounded * price
    
    if final_usdt_amount < min_cost * 0.99: # 誤差を考慮
        logging.warning(f"⚠️ {symbol}: 最終投入額が最小ロットを下回りました。再チェックが必要です。")
        # 最小ロットチェックを通過したはずなので、CCXTの丸め処理による極端な誤差でなければ許容する
        
    return base_amount_rounded, final_usdt_amount


def get_fgi_data() -> Tuple[Optional[int], Optional[float]]:
    """Alternative Fear & Greed Index (FGI) プロキシ値を取得する (同期)"""
    try:
        # FGI APIのURL (例: Alternative.meのAPI)
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=5)
        response.raise_for_status() # HTTPエラーの場合に例外を発生させる
        
        data = response.json()
        
        if data and 'data' in data and data['data']:
            fgi_raw_value = int(data['data'][0]['value']) # 0-100の数値
            
            # FGIプロキシの計算: 50を基準として、-0.5から+0.5に正規化
            # (FGI - 50) / 100 * (FGI_PROXY_BONUS_MAX / 0.5)
            # 簡略化して [-0.5, 0.5] に正規化する
            fgi_proxy = (fgi_raw_value - 50) / 100.0 * 2.0 * FGI_PROXY_BONUS_MAX
            
            return fgi_raw_value, fgi_proxy
        
        return None, None
        
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生: {e}")
        return None, None

def calculate_forex_bonus(usdbrl_close: float, usdjpy_close: float) -> float:
    """USD/BRLとUSD/JPYの終値から為替ボーナスを計算する (同期)"""
    try:
        # このロジックは、USD/BRL(新興国リスク)とUSD/JPY(安全資産逃避)の変動から
        # グローバルなリスクオン/リスクオフを推測する。
        # USD/JPYの上昇(円安) = リスクオン (ボーナス)
        # USD/BRLの低下(ブラジルレアル高) = リスクオン (ボーナス)
        
        # 基準値 (このロジックは完全な実装ではないため、デモ用のロジックを使用)
        base_usdjpy = 145.0
        base_usdbrl = 5.0
        
        # JPYの相対的な変化 (安全資産逃避の逆)
        jpy_factor = (usdjpy_close - base_usdjpy) / base_usdjpy
        
        # BRLの相対的な変化 (新興国リスクの逆)
        brl_factor = (base_usdbrl - usdbrl_close) / base_usdbrl
        
        # 2つのファクターの平均をボーナスとする (最大 0.5 * FGI_PROXY_BONUS_MAX)
        forex_bonus = (jpy_factor + brl_factor) / 2.0
        
        # ボーナスを最大値にクリッピング
        max_bonus = FGI_PROXY_BONUS_MAX / 2.0
        forex_bonus = min(max(forex_bonus, -max_bonus), max_bonus)
        
        return forex_bonus
        
    except Exception as e:
        logging.error(f"❌ 為替ボーナス計算中にエラーが発生: {e}")
        return 0.0


async def fetch_fgi_data() -> Dict:
    """FGIと為替データ(forex_bonus)を取得する"""
    global GLOBAL_MACRO_CONTEXT, LAST_FOREX_FETCH_TIME, FOREX_CACHE, FOREX_COOLDOWN
    
    # 1. FGIプロキシの取得 (常に最新を取得)
    # requestsは同期的なので、asyncio.to_threadを使用
    fgi_raw_value, fgi_proxy = await asyncio.to_thread(get_fgi_data)
    
    if fgi_raw_value is None:
        # FGI取得失敗時は前回の値を維持
        fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
        fgi_raw_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
        logging.error(f"❌ FGIデータ取得に失敗。前回の値を使用: Raw Value={fgi_raw_value}, Proxy={fgi_proxy:.4f}")
    else:
        logging.info(f"✅ FGIプロキシ取得成功: Raw Value={fgi_raw_value}, Proxy={fgi_proxy:.4f}")
    
    # 2. 為替データ (forex_bonus) の計算 (Yfinanceを使用)
    forex_bonus = 0.0
    
    # ★ 修正 (Fix 2): Yfinance レート制限対策として、60分に1回のみ取得を試みる
    if time.time() - LAST_FOREX_FETCH_TIME > FOREX_COOLDOWN or LAST_FOREX_FETCH_TIME == 0.0:
        logging.info("⏳ Yfinance為替データ取得を試行中...")
        try:
            if yf:
                # Yfinanceは同期処理なので、asyncio.to_threadで別スレッドで実行
                def fetch_forex_sync():
                    # USD/JPY の為替レートの取得
                    usdjpy_ticker = yf.Ticker("USDJPY=X")
                    usdjpy_data = usdjpy_ticker.history(period="1d", interval="1m")
                    usdjpy_close = usdjpy_data['Close'].iloc[-1]
                    
                    # USD/BRL の為替レートの取得
                    usdbrl_ticker = yf.Ticker("USDBRL=X")
                    usdbrl_data = usdbrl_ticker.history(period="1d", interval="1m")
                    usdbrl_close = usdbrl_data['Close'].iloc[-1]
                    
                    return usdbrl_close, usdjpy_close
                
                usdbrl_close, usdjpy_close = await asyncio.to_thread(fetch_forex_sync)
                
                forex_bonus = calculate_forex_bonus(usdbrl_close, usdjpy_close)
                
                # 成功した場合のみ時刻とキャッシュを更新
                LAST_FOREX_FETCH_TIME = time.time()
                FOREX_CACHE['forex_bonus'] = forex_bonus
                logging.info(f"✅ Yfinance為替データ取得成功: Bonus={forex_bonus:.4f}")
            else:
                 raise Exception("yfinanceライブラリがロードされていません。")
            
        except Exception as e:
            # Yfinanceのレート制限エラーをキャッチ
            logging.error(f"❌ Yfinance為替データ取得中にエラーが発生: {e}")
            # エラーが発生した場合でも、最新のキャッシュ値を使用する
            forex_bonus = FOREX_CACHE['forex_bonus']
            logging.warning(f"⚠️ Yfinanceデータ取得失敗。キャッシュされた為替データ Bonus={forex_bonus:.4f} を使用します。")
            # 失敗時は LAST_FOREX_FETCH_TIME を更新しないため、次回クールダウン後に再試行される

    else:
        # クールダウン中の場合はキャッシュを使用
        forex_bonus = FOREX_CACHE['forex_bonus']
        logging.info(f"ℹ️ Yfinance為替データ取得をスキップしました (クールダウン中)。キャッシュ値: Bonus={forex_bonus:.4f}")


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
    # 既存のcolumnsを上書きしないように、計算前にコピーを作成
    # df = df.copy() # 既にfetch_ohlcv_and_analyzeでコピー済み
    
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
    if bb_data is not None and not bb_data.empty:
        # キーを動的に取得
        bb_keys = [col for col in bb_data.columns if 'BBL' in col or 'BBM' in col or 'BBU' in col]
        if len(bb_keys) >= 3:
            df['BBL'] = bb_data[bb_keys[0]]
            df['BBM'] = bb_data[bb_keys[1]]
            df['BBU'] = bb_data[bb_keys[2]]
        else:
            df['BBL'] = np.nan
            df['BBM'] = np.nan
            df['BBU'] = np.nan
    else:
        df['BBL'] = np.nan
        df['BBM'] = np.nan
        df['BBU'] = np.nan
        
    # Average True Range (ATR)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    return df

# ... (analyze_symbol, get_candlestick_data, fetch_ohlcv_and_analyze, calculate_signal のロジックは省略)

def calculate_signal(
    market_ticker: Dict, 
    ohlcv_df: pd.DataFrame, 
    timeframe: str, 
    macro_context: Dict
) -> Optional[Dict]:
    """
    指定されたデータフレームからシグナルを計算し、スコアリングを行う。
    ロングエントリーシグナルのみを返す。
    """
    
    # データフレームが不十分な場合
    if ohlcv_df.empty or len(ohlcv_df) < LONG_TERM_SMA_LENGTH + 1: # SMA200+1のデータが必要
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): データ不足 ({len(ohlcv_df)}/{LONG_TERM_SMA_LENGTH + 1})")
        return None
        
    # 最新のローソク足の情報を取得
    last_candle = ohlcv_df.iloc[-1]
    
    # 最終的なスコア計算に必要な指標がNaNであれば除外
    if last_candle.isnull().any():
        # logging.warning(f"⚠️ {market_ticker['symbol']} ({timeframe}): 最新のローソク足にNaNが含まれています。")
        return None

    current_price = market_ticker['last']
    
    # 1. ロングエントリーの基本条件: 価格が中期のSMA (例: SMA50) を上回っている
    is_above_mid_term_sma = current_price > last_candle['SMA_50']
    
    # 2. 長期トレンドフィルター: 価格が長期のSMA (例: SMA200) を上回っている
    is_above_long_term_sma = current_price > last_candle['SMA_200']
    
    # 3. リスクリワード比の計算
    
    # エントリー価格: 最新の終値 (または指値戦略として current_price を使用)
    entry_price = last_candle['close'] 
    
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

    # D. 価格構造/ピボット支持ボーナス (6点)
    # 価格がSMA50を上回っていること (トレンドフォローの基本)
    structural_pivot_bonus = 0.0
    if is_above_mid_term_sma:
        structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
    total_score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # E. MACDペナルティ (25点)
    # MACDラインがシグナルラインを下回っている、またはヒストグラムが負で減少している場合にペナルティ
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインを下回る (デッドクロス)
    is_dead_cross = last_candle['MACD'] < last_candle['MACDs']
    # ヒストグラムが負で、かつ直前のヒストグラムよりも負の度合いが深まっている (勢いの加速)
    is_momentum_decelerating = (last_candle['MACDh'] < 0) and (ohlcv_df['MACDh'].iloc[-2] is not np.nan and last_candle['MACDh'] < ohlcv_df['MACDh'].iloc[-2])
    
    if is_dead_cross or is_momentum_decelerating:
        macd_penalty_value = MACD_CROSS_PENALTY
    total_score -= macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # F. RSIモメンタムボーナス (10点)
    # RSIが45以下から反転している、またはRSIが50を上回って加速している場合にボーナス
    rsi_momentum_bonus_value = 0.0
    rsi_value = last_candle['RSI']
    tech_data['rsi_value'] = rsi_value # RSI値を記録

    if rsi_value > 55: # RSIが55を超えて加速している
        # 55-70の範囲で線形にボーナスを適用
        max_rsi = 70.0
        min_rsi = 55.0
        if rsi_value < max_rsi:
            ratio = (rsi_value - min_rsi) / (max_rsi - min_rsi)
        else:
            ratio = 1.0
            
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ratio
    
    total_score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # G. 出来高/OBV確証ボーナス (5点)
    # OBVが上昇傾向にある (出来高による価格上昇の確証)
    obv_momentum_bonus_value = 0.0
    # OBVのSMA20が直近のOBVを下回っている (OBVが長期的に上昇トレンド)
    obv_sma = ta.sma(ohlcv_df['OBV'], length=20).iloc[-1]
    if last_candle['OBV'] > obv_sma:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    total_score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # H. 出来高スパイクボーナス (7点)
    # 直近の出来高が過去20期間の出来高平均を大きく上回る (例: 2倍以上)
    volume_increase_bonus_value = 0.0
    avg_volume = ohlcv_df['volume'].iloc[-20:-1].mean() # 直近20期間 (最新除く)
    current_volume = last_candle['volume']
    
    if avg_volume > 0 and current_volume > (avg_volume * 2.0): # 2倍以上
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS
        
    total_score += volume_increase_bonus_value
    tech_data['volume_increase_bonus_value'] = volume_increase_bonus_value
    
    # I. 低ボラティリティペナルティ (ボリンジャーバンド幅による)
    volatility_penalty_value = 0.0
    # BB幅の計算 ( (BBU - BBL) / BBM )
    if last_candle['BBM'] > 0 and not np.isnan(last_candle['BBU']) and not np.isnan(last_candle['BBL']):
        bb_width_ratio = (last_candle['BBU'] - last_candle['BBL']) / last_candle['BBM']
        if bb_width_ratio < VOLATILITY_BB_PENALTY_THRESHOLD: # 1%未満
            volatility_penalty_value = -0.15 # 15点のペナルティ
            
    total_score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # J. 流動性ボーナス (7点)
    # 取引所の板情報 (depth) を取得する必要があるが、ここではティッカーから出来高を使用する
    # 24H出来高の変動率が上位20%であればボーナスを適用する (簡易的な流動性評価)
    # ここでは、簡略化のため、市場平均の出来高を上回っていることとする
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX # 常に最大値を与える（実際の流動性チェックは困難なため）
    
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
    
    # 4. スコアに基づいてロットサイズを線形補間
    
    # スコアが最低閾値 (例: 0.80) 未満の場合は、最小ロットに制限 (この関数に来る前に閾値でフィルタリングされているはず)
    # 最低ロットのベースラインを 0.60 と定義する
    lot_base_score = 0.60
    
    if score <= lot_base_score:
        lot_size = min_lot_from_equity
    elif score >= DYNAMIC_LOT_SCORE_MAX:
        lot_size = max_lot_from_equity
    else:
        # スコア (S) を [lot_base_score, DYNAMIC_LOT_SCORE_MAX] の範囲で正規化
        ratio = (score - lot_base_score) / (DYNAMIC_LOT_SCORE_MAX - lot_base_score)
        
        # ロットサイズ (L) を [min_lot_from_equity, max_lot_from_equity] の範囲で線形補間
        lot_size = min_lot_from_equity + (max_lot_from_equity - min_lot_from_equity) * ratio
        
    # 5. 最終ロットサイズの決定
    # 計算されたロットサイズは、最小ロット以上かつ最大ロット以下に制限される
    final_lot_size = max(min_usdt_lot, min(lot_size, max_lot_from_equity))
    
    # 最小ロット額よりも、総資産に基づいた最小割合を優先する
    final_lot_size = max(min_lot_from_equity, final_lot_size)

    return final_lot_size

async def fetch_account_status() -> Dict:
    """口座のUSDT残高、総資産、オープンポジションを取得する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが準備されていません。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

    try:
        # 1. CCXTのfetch_balanceを使用して残高を取得
        total_usdt_balance = 0.0
        total_equity = 0.0

        # ★ 修正 (Fix 1): MEXC NotSupported エラー対策
        fetch_params = {}
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCのSpot取引残高を取得するために type='spot' を明示的に渡す
            fetch_params['type'] = 'spot'
            
        balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
        
        # 2. 残高データからUSDTの利用可能残高を取得
        # total, free, used のどれもNoneでないことを確認
        total_usdt_balance = balance.get('free', {}).get('USDT', 0.0)
        
        if total_usdt_balance is None:
             total_usdt_balance = 0.0
             
        # 3. 総資産額 (Equity) の計算 (USDT残高 + USDT以外の保有資産の時価評価額)
        total_equity = total_usdt_balance + balance.get('used', {}).get('USDT', 0.0) # USDTの合計残高
        
        # USDT以外の保有資産をUSDT建てで評価
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001: # 0でない保有資産
                try:
                    symbol = f"{currency}/USDT"
                    # シンボルが取引所に存在するか確認し、存在しない場合はハイフンなしの形式も試す
                    if symbol not in EXCHANGE_CLIENT.markets:
                        if f"{currency}USDT" in EXCHANGE_CLIENT.markets:
                            symbol = f"{currency}USDT"
                        else:
                            continue # 取引所で扱っていない銘柄はスキップ
                            
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
        
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}


async def fetch_top_symbols() -> List[str]:
    """出来高TOPの銘柄リストを取得する"""
    global EXCHANGE_CLIENT, TOP_SYMBOL_LIMIT, DEFAULT_SYMBOLS
    logging.info("⏳ 出来高TOP銘柄を取得中...")
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが準備されていません。デフォルト銘柄を使用します。")
        return DEFAULT_SYMBOLS

    try:
        # 1. 全ティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # ★ 修正 (Fix 3): fetch_tickersがNoneを返す可能性があるためチェックを追加 (ログのエラーに対応)
        if not tickers or not isinstance(tickers, dict):
            # 'NoneType' object has no attribute 'keys' エラーに対応
            logging.error("❌ ティッカー情報の取得に失敗。返り値がNoneまたは不正です。デフォルト銘柄を使用。")
            return DEFAULT_SYMBOLS
            
        # 2. 現物/USDTペアのみをフィルタリングし、出来高順にソート
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers.items()
            if symbol.endswith('/USDT') or symbol.endswith('USDT') # USDTペアであること
            and ticker and 'quoteVolume' in ticker and ticker['quoteVolume'] is not None # quoteVolume (USDT出来高)があること
            and EXCHANGE_CLIENT.markets.get(symbol, {}).get('spot') # 現物市場であること
        }

        # 出来高 (quoteVolume) 順にソート (降順)
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # 3. TOP Nの銘柄を取得
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        logging.info(f"✅ 出来高TOP銘柄の取得に成功しました。監視対象: {len(top_symbols)} 銘柄。")
        
        # 最小でもデフォルト銘柄を含める
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        return final_symbols

    except Exception as e:
        logging.error(f"❌ ティッカー情報の取得に失敗。デフォルト銘柄を使用: {e}", exc_info=True)
        return DEFAULT_SYMBOLS
        
# ------------------------------------------------------------------------------------
# 以下、完全なコードに必須な関数群 (省略せずに含める)
# ------------------------------------------------------------------------------------

async def get_candlestick_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """CCXTクライアントからOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ {symbol} OHLCV取得失敗: CCXTクライアントが準備されていません。")
        return None

    try:
        # fetch_ohlcv は非同期メソッド
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol, 
            timeframe=timeframe, 
            limit=limit
        )
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが取得できませんでした。")
            return None
            
        # DataFrameに変換
        df = pd.DataFrame(
            ohlcv, 
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        
        # timestampをdatetime型に変換
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        
        # インデックスを設定
        df.set_index('timestamp', inplace=True)
        
        # 数値型をfloatに変換
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        # 最新のデータが不完全な場合（最終ローソク足が途中の場合）は、
        # 最新のものを含めず、直前までのデータを使用するロジックが必要だが、
        # ここでは単純に最新のローソク足まで含めるものとする。
        
        # 指標計算に必要な期間のデータがあるか確認
        required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        if len(df) < required_limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): データが少なすぎます ({len(df)}/{required_limit})。")
            return None
        
        return df
        
    except Exception as e:
        logging.error(f"❌ {symbol} OHLCVデータ取得中にエラーが発生 ({timeframe}): {e}", exc_info=True)
        return None

async def fetch_ohlcv_and_analyze(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、テクニカル指標を計算する"""
    # 必要なデータ量 (最大のSMA200+αを考慮)
    limit = max(REQUIRED_OHLCV_LIMITS[timeframe], LONG_TERM_SMA_LENGTH + 50) 
    
    df = await get_candlestick_data(symbol, timeframe, limit)
    
    if df is None or df.empty:
        return None
        
    # テクニカル指標の計算
    df = calculate_indicators(df.copy()) # コピーを渡して元のDataFrameを保護
    
    return df

async def analyze_symbol(symbol: str, market_ticker: Dict, macro_context: Dict) -> List[Dict]:
    """
    指定された銘柄の複数時間足で分析を行い、シグナルを収集する。
    """
    signals = []
    
    for timeframe in TARGET_TIMEFRAMES:
        # OHLCVデータを取得し、テクニカル指標を計算
        ohlcv_df = await fetch_ohlcv_and_analyze(symbol, timeframe)
        
        if ohlcv_df is None:
            continue
            
        # シグナルを計算
        signal = calculate_signal(market_ticker, ohlcv_df, timeframe, macro_context)
        
        if signal:
            # 動的ロットサイズの計算
            # 注: analyze_symbolは取引実行前のため、account_statusは最新でない可能性があるが、
            # execute_tradeで再計算されるため、ここでは概算としてGLOBAL_TOTAL_EQUITYを使用
            account_status_for_lot = {'total_equity': GLOBAL_TOTAL_EQUITY}
            lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status_for_lot)
            signal['lot_size_usdt'] = lot_size_usdt
            
            signals.append(signal)
            
    return signals

async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """
    取引を実行する。現物買い（指値IOC）と同時にSL/TPの指値売り注文を設定する。
    """
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY, OPEN_POSITIONS
    symbol = signal['symbol']
    
    if TEST_MODE:
        logging.info(f"💡 TEST MODE: {symbol} の取引実行をスキップします。")
        return {'status': 'ok', 'filled_amount': 0.0, 'filled_usdt': signal['lot_size_usdt'], 'error_message': 'TEST MODE - NO TRADE'}

    # 1. 注文数量の再計算とリソースチェック
    entry_price = signal['entry_price']
    
    # 最新の総資産額に基づいてロットサイズを再計算
    new_lot_size_usdt = calculate_dynamic_lot_size(signal['score'], account_status)
    
    # 注文数量を取引所の精度に基づいて調整
    amount_to_buy, final_usdt_cost = await adjust_order_amount(
        symbol, 
        new_lot_size_usdt, 
        entry_price
    )
    
    # 最終的なロットサイズをシグナルに反映
    signal['lot_size_usdt'] = final_usdt_cost 
    
    if amount_to_buy <= 0 or final_usdt_cost > account_status['total_usdt_balance']:
        error_msg = f"ロットサイズ ({final_usdt_cost:.2f} USDT) が最小ロットまたは残高 ({account_status['total_usdt_balance']:.2f} USDT) を超えています。"
        logging.error(f"❌ TRADE SKIPPED: {symbol} - {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
        
    
    # 2. メインの買い注文 (指値IOC)
    try:
        # IOC (Immediate Or Cancel) 指値買い注文
        # IOCは、即時に約定可能な分だけ約定させ、残りをキャンセルする注文
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',      # 指値注文
            side='buy',
            amount=amount_to_buy,
            price=entry_price,
            params={'timeInForce': 'IOC'} # 即時約定・不約定分キャンセル
        )
        
        # 注文情報をログに記録
        logging.info(f"✅ {symbol}: 指値買い注文 (IOC) を送信しました。ID: {order['id']}")
        
    except Exception as e:
        error_msg = f"指値買い注文の送信に失敗: {e}"
        logging.error(f"❌ TRADE FAILED: {symbol} - {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}

    # 3. 注文が部分的にでも約定したかを確認
    filled_amount = order.get('filled', 0.0)
    filled_usdt = order.get('cost', 0.0)
    
    if filled_amount <= 0.0:
        # 約定がゼロの場合、取引失敗と判断
        logging.warning(f"⚠️ {symbol}: 指値買い注文が約定しませんでした。取引をスキップします。")
        # SL/TP注文も出す必要がないので、ここで終了
        return {'status': 'error', 'error_message': '指値買い注文が約定しなかったためキャンセルされました。', 'close_status': 'skipped'}

    # 4. SL/TP注文の数量調整と価格設定
    
    # SL/TPの数量は、約定した数量 (filled_amount) に合わせる
    sl_price = signal['stop_loss']
    tp_price = signal['take_profit']
    
    # 取引所の価格精度に合わせてSL/TP価格を調整
    sl_price_precision = EXCHANGE_CLIENT.price_to_precision(symbol, sl_price)
    tp_price_precision = EXCHANGE_CLIENT.price_to_precision(symbol, tp_price)
    
    sl_order_id = None
    tp_order_id = None
    
    # 5. SL（ストップロス）指値売り注文の設定 (Good Til Canceled: GTC)
    try:
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='sell',
            amount=filled_amount,
            price=sl_price_precision,
            params={'timeInForce': 'GTC'} 
        )
        sl_order_id = sl_order.get('id')
        logging.info(f"✅ {symbol}: SL指値売り注文を送信しました。ID: {sl_order_id}")
        
    except Exception as e:
        logging.error(f"❌ {symbol}: SL注文の設定に失敗しました: {e}")
        # SL注文失敗は重大なので、強制的にポジションをクローズする
        return await _emergency_close_position(symbol, filled_amount, filled_usdt, entry_price)

    # 6. TP（テイクプロフィット）指値売り注文の設定 (GTC)
    try:
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='sell',
            amount=filled_amount,
            price=tp_price_precision,
            params={'timeInForce': 'GTC'} 
        )
        tp_order_id = tp_order.get('id')
        logging.info(f"✅ {symbol}: TP指値売り注文を送信しました。ID: {tp_order_id}")
        
    except Exception as e:
        logging.error(f"❌ {symbol}: TP注文の設定に失敗しました: {e}")
        # TP注文失敗は許容できる場合もあるが、SLとTPはペアで管理すべきなので、
        # SL注文をキャンセルし、ポジションをクローズする
        try:
            if sl_order_id:
                await EXCHANGE_CLIENT.cancel_order(sl_order_id, symbol)
        except Exception as cancel_e:
            logging.error(f"❌ {symbol}: TP失敗後のSLキャンセルに失敗: {cancel_e}")
        
        return await _emergency_close_position(symbol, filled_amount, filled_usdt, entry_price)

    # 7. ポジション情報を管理リストに追加
    OPEN_POSITIONS.append({
        'symbol': symbol,
        'entry_price': entry_price, # 注文価格 (平均約定価格ではない)
        'stop_loss': float(sl_price_precision), # 注文価格 (精度調整後)
        'take_profit': float(tp_price_precision), # 注文価格 (精度調整後)
        'filled_amount': filled_amount,
        'filled_usdt': filled_usdt,
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
        'open_time': time.time(),
        'uuid': str(uuid.uuid4()) # ユニークID
    })

    # 成功結果を返す
    return {
        'status': 'ok',
        'filled_amount': filled_amount,
        'filled_usdt': filled_usdt,
        'entry_price': entry_price,
        'stop_loss': float(sl_price_precision),
        'take_profit': float(tp_price_precision),
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
    }

async def _emergency_close_position(symbol: str, filled_amount: float, filled_usdt: float, entry_price: float) -> Dict:
    """注文失敗時にポジションを成行で強制クローズする"""
    global EXCHANGE_CLIENT
    
    close_status = 'skipped'
    close_error_message = None
    closed_amount = 0.0
    
    if filled_amount > 0:
        try:
            # 成行売り注文
            close_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=filled_amount
            )
            
            closed_amount = close_order.get('filled', 0.0)
            close_status = 'ok'
            logging.warning(f"⚠️ {symbol}: 不完全ポジションを成行で強制クローズしました。約定数量: {closed_amount:.4f}")

        except Exception as close_e:
            close_status = 'error'
            close_error_message = str(close_e)
            logging.critical(f"🚨 {symbol}: 強制クローズに失敗しました。ポジションが残っている可能性があります。: {close_e}")
            
    # 取引失敗のメッセージを返す
    return {
        'status': 'error',
        'error_message': 'SL/TP注文の設定に失敗したため、不完全ポジションをクローズしました。',
        'close_status': close_status,
        'closed_amount': closed_amount,
        'close_error_message': close_error_message,
        'filled_usdt': filled_usdt,
        'entry_price': entry_price,
    }


async def open_order_management_loop():
    """オープンポジションとSL/TP注文を監視するループ (10秒ごと)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS, GLOBAL_TOTAL_EQUITY
    
    # 💡 CCXTクライアントが準備できるまで待機
    while not IS_CLIENT_READY:
        await asyncio.sleep(5)
        
    while True:
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒待機
        
        # 監視対象がない場合はスキップ
        if not OPEN_POSITIONS:
            continue
            
        logging.debug(f"🔍 オープンポジションを監視中... ({len(OPEN_POSITIONS)} 銘柄)")

        positions_to_remove = []

        for position in OPEN_POSITIONS:
            symbol = position['symbol']
            sl_id = position.get('sl_order_id')
            tp_id = position.get('tp_order_id')
            
            # --- 1. SL/TP注文のステータスチェック ---
            
            # SL注文が約定したか確認
            is_sl_filled = False
            if sl_id:
                try:
                    sl_order = await EXCHANGE_CLIENT.fetch_order(sl_id, symbol)
                    if sl_order['status'] == 'closed':
                        is_sl_filled = True
                        position['exit_price'] = sl_order['average'] # 平均約定価格を記録
                        position['exit_type'] = 'Stop Loss'
                        logging.info(f"🛑 {symbol}: SL注文 ({sl_id}) が約定しました。")
                except ccxt.base.errors.OrderNotFound:
                    logging.warning(f"⚠️ {symbol}: SL注文が見つかりません ({sl_id})。手動キャンセルされた可能性があります。")
                except Exception as e:
                    logging.error(f"❌ {symbol}: SL注文の取得に失敗: {e}")

            # TP注文が約定したか確認
            is_tp_filled = False
            if tp_id:
                try:
                    tp_order = await EXCHANGE_CLIENT.fetch_order(tp_id, symbol)
                    if tp_order['status'] == 'closed':
                        is_tp_filled = True
                        position['exit_price'] = tp_order['average'] # 平均約定価格を記録
                        position['exit_type'] = 'Take Profit'
                        logging.info(f"🛑 {symbol}: TP注文 ({tp_id}) が約定しました。")
                except ccxt.base.errors.OrderNotFound:
                    logging.warning(f"⚠️ {symbol}: TP注文が見つかりません ({tp_id})。手動キャンセルされた可能性があります。")
                except Exception as e:
                    logging.error(f"❌ {symbol}: TP注文の取得に失敗: {e}")
            
            # --- 2. 決済処理 ---
            
            if is_sl_filled or is_tp_filled:
                
                # 約定しなかった残りの注文をキャンセル
                if is_sl_filled and tp_id:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(tp_id, symbol)
                    except Exception as e:
                        logging.warning(f"⚠️ {symbol}: TP注文のキャンセルに失敗 (SL約定後): {e}")
                elif is_tp_filled and sl_id:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(sl_id, symbol)
                    except Exception as e:
                        logging.warning(f"⚠️ {symbol}: SL注文のキャンセルに失敗 (TP約定後): {e}")

                # 実現損益 (PnL) の計算
                entry_price = position['entry_price']
                exit_price = position.get('exit_price', 0.0)
                amount = position['filled_amount']
                filled_usdt = position['filled_usdt']

                pnl_usdt = (exit_price - entry_price) * amount
                pnl_percent = (pnl_usdt / filled_usdt) * 100 if filled_usdt > 0 else 0.0
                
                # 通知用シグナルを作成
                result_signal = {
                    'symbol': symbol,
                    'timeframe': 'Managed', # 管理対象であること
                    'score': 1.0, # 決済済みなので最高スコア
                    'entry_price': entry_price,
                    'stop_loss': position['stop_loss'],
                    'take_profit': position['take_profit'],
                    'rr_ratio': round((position['take_profit'] - entry_price) / (entry_price - position['stop_loss']), 2),
                    'pnl_usdt': pnl_usdt,
                    'pnl_percent': pnl_percent,
                    'exit_price': exit_price,
                    'exit_type': position['exit_type'],
                }
                
                # PnLを総資産に反映し、最新のステータスを取得（取引所API負荷を減らすため、必ずしもここで最新化しないが、今回は実装を簡略化）
                # ここでは単純にGLOBAL_TOTAL_EQUITYを更新
                # 正確には、次のメインループでfetch_account_statusが実行される際に更新される
                
                # PnLを通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                message = format_telegram_message(result_signal, "ポジション決済", current_threshold, result_signal, position['exit_type'])
                asyncio.create_task(send_telegram_notification(message))
                log_signal(result_signal, "ポジション決済")

                positions_to_remove.append(position)
                
            # --- 3. SL/TP注文の存在チェックと再設定 (V19.0.53 Feature) ---
            else:
                # 片方の注文IDがない（例: 手動キャンセルされた、またはAPIエラーで片方のみ失敗した）場合
                has_sl = sl_id is not None
                has_tp = tp_id is not None
                
                if (has_sl and not has_tp) or (not has_sl and has_tp) or (not has_sl and not has_tp):
                    
                    # 注文再設定のロジック
                    entry_price = position['entry_price']
                    filled_amount = position['filled_amount']
                    sl_price = position['stop_loss']
                    tp_price = position['take_profit']
                    
                    logging.warning(f"⚠️ {symbol}: SL/TP注文のいずれかが見つかりません (SL:{has_sl}, TP:{has_tp})。再設定を試行します。")
                    
                    # 既存の残っている注文をキャンセル
                    if sl_id:
                        try:
                            await EXCHANGE_CLIENT.cancel_order(sl_id, symbol)
                            position['sl_order_id'] = None
                        except Exception:
                            logging.warning(f"⚠️ {symbol}: 既存SL注文 ({sl_id}) のキャンセルに失敗。")
                            
                    if tp_id:
                        try:
                            await EXCHANGE_CLIENT.cancel_order(tp_id, symbol)
                            position['tp_order_id'] = None
                        except Exception:
                            logging.warning(f"⚠️ {symbol}: 既存TP注文 ({tp_id}) のキャンセルに失敗。")

                    # SL/TP価格の精度調整
                    sl_price_precision = EXCHANGE_CLIENT.price_to_precision(symbol, sl_price)
                    tp_price_precision = EXCHANGE_CLIENT.price_to_precision(symbol, tp_price)
                    
                    # SL注文の再設定
                    try:
                        sl_order = await EXCHANGE_CLIENT.create_order(
                            symbol=symbol, type='limit', side='sell', amount=filled_amount, 
                            price=sl_price_precision, params={'timeInForce': 'GTC'} 
                        )
                        position['sl_order_id'] = sl_order.get('id')
                        logging.info(f"✅ {symbol}: SL注文を再設定しました。ID: {sl_order.get('id')}")
                    except Exception as e:
                        logging.error(f"❌ {symbol}: SL注文の再設定に失敗: {e}")

                    # TP注文の再設定
                    try:
                        tp_order = await EXCHANGE_CLIENT.create_order(
                            symbol=symbol, type='limit', side='sell', amount=filled_amount, 
                            price=tp_price_precision, params={'timeInForce': 'GTC'} 
                        )
                        position['tp_order_id'] = tp_order.get('id')
                        logging.info(f"✅ {symbol}: TP注文を再設定しました。ID: {tp_order.get('id')}")
                    except Exception as e:
                        logging.error(f"❌ {symbol}: TP注文の再設定に失敗: {e}")


        # 決済が完了したポジションをリストから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]


async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG, HOURLY_ATTEMPT_LOG, BOT_VERSION
    
    # 💡 CCXTクライアントが準備できるまで待機
    while not IS_CLIENT_READY:
        await asyncio.sleep(5)
    
    while True:
        try:
            start_time = time.time()
            now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
            logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")
            
            # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
            GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 2. 口座ステータスを取得し、新規取引の可否をチェック
            account_status = await fetch_account_status()
            can_trade = not account_status.get('error') and account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE and not TEST_MODE
            
            if account_status.get('error'):
                logging.critical("🚨 口座ステータスの取得に失敗しました。取引をスキップします。")
            elif not can_trade and not TEST_MODE:
                logging.warning(f"⚠️ USDT残高 ({account_status['total_usdt_balance']:.2f}) が最小取引可能額 ({MIN_USDT_BALANCE_FOR_TRADE:.2f}) 未満です。取引をスキップします。")

            # 3. 出来高上位銘柄を更新 (1時間ごと)
            if time.time() - LAST_SUCCESS_TIME > 60 * 60:
                CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols()
                LAST_SUCCESS_TIME = time.time()
                # ログとシグナル記録をリセット
                HOURLY_SIGNAL_LOG = [] 
                HOURLY_ATTEMPT_LOG = {}
            
            # 4. 分析と取引の実行
            logging.info(f"⏳ {len(CURRENT_MONITOR_SYMBOLS) * len(TARGET_TIMEFRAMES)} 個のOHLCVデータ取得と分析タスクを実行中.")
            
            # 並行して分析を実行
            analysis_tasks = [
                analyze_symbol(symbol, await EXCHANGE_CLIENT.fetch_ticker(symbol), GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
                if symbol not in [p['symbol'] for p in OPEN_POSITIONS] # オープンポジションがない銘柄のみ
            ]
            
            # 既にポジションがある銘柄をカウント
            positions_in_analysis_count = len(CURRENT_MONITOR_SYMBOLS) - len(analysis_tasks)
            if positions_in_analysis_count > 0:
                 HOURLY_ATTEMPT_LOG['In_Position'] = HOURLY_ATTEMPT_LOG.get('In_Position', 0) + positions_in_analysis_count
                 
            
            all_signals_nested = await asyncio.gather(*analysis_tasks, return_exceptions=True)
            
            # 結果をフラット化し、エラーをフィルタリング
            all_signals = []
            for result in all_signals_nested:
                if isinstance(result, Exception):
                    # logging.error(f"❌ 分析タスク中にエラーが発生: {result}") # analyze_symbol内のエラーは既にログ済み
                    continue
                all_signals.extend(result)
            
            # スコア順にソートし、閾値以上のものを抽出
            valid_signals = sorted(
                [s for s in all_signals if s['score'] >= current_threshold],
                key=lambda s: s['score'],
                reverse=True
            )
            
            # 5. 取引シグナル処理と取引実行
            
            # 処理済みのシグナル（クールダウン中のシグナル）をフィルタリング
            new_signals_for_trade = []
            for signal in valid_signals:
                symbol = signal['symbol']
                
                # クールダウンチェック
                last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
                if (time.time() - last_signal_time) > TRADE_SIGNAL_COOLDOWN:
                    new_signals_for_trade.append(signal)
                    
            # 取引は、最もスコアの高いシグナル一つだけを実行
            if new_signals_for_trade and can_trade:
                best_signal = new_signals_for_trade[0]
                
                # 取引実行
                trade_result = await execute_trade(best_signal, account_status)
                
                # ログ/通知
                if trade_result['status'] == 'ok':
                    logging.info(f"🎉 TRADE SUCCESS: {best_signal['symbol']} - Score: {best_signal['score']:.4f}")
                    # ログと通知用に取引結果をシグナルに統合
                    signal_with_result = {**best_signal, **trade_result}
                    
                    message = format_telegram_message(signal_with_result, "取引シグナル", current_threshold, trade_result)
                    asyncio.create_task(send_telegram_notification(message))
                    log_signal(signal_with_result, "取引シグナル")
                    
                    # クールダウン時間を更新
                    LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                
                elif trade_result['status'] == 'error':
                    logging.error(f"❌ TRADE FAILED: {best_signal['symbol']} - {trade_result['error_message']}")
                    # ログと通知
                    signal_with_result = {**best_signal, **trade_result}
                    
                    message = format_telegram_message(signal_with_result, "取引シグナル", current_threshold, trade_result)
                    asyncio.create_task(send_telegram_notification(message))
                    log_signal(signal_with_result, "取引シグナル")

            # 6. Hourly Reportの処理
            
            # 有効なシグナルをログに追加
            for signal in valid_signals:
                 HOURLY_SIGNAL_LOG.append(signal)
            
            # 初回ループ完了通知の送信
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                 # 初回通知は、口座ステータス取得エラーがない場合にのみ送信
                 if not account_status.get('error'):
                    startup_msg = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold, BOT_VERSION)
                    asyncio.create_task(send_telegram_notification(startup_msg))
                    IS_FIRST_MAIN_LOOP_COMPLETED = True
                    
            # 1時間ごとのレポート通知
            if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
                 # 重複を削除してソート (最高のスコアのみを残す)
                 unique_signals = {}
                 for signal in HOURLY_SIGNAL_LOG:
                     symbol_tf = (signal['symbol'], signal['timeframe'])
                     if symbol_tf not in unique_signals or signal['score'] > unique_signals[symbol_tf]['score']:
                         unique_signals[symbol_tf] = signal
                         
                 report_msg = format_hourly_report(list(unique_signals.values()), HOURLY_ATTEMPT_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold, BOT_VERSION)
                 asyncio.create_task(send_telegram_notification(report_msg))
                 
                 # 通知時刻を更新
                 LAST_HOURLY_NOTIFICATION_TIME = time.time()
                 # ログとシグナル記録は、次のTOP銘柄更新時にリセットされる (ここではリセットしない)
                 
            
            # 7. 次のループまで待機
            elapsed_time = time.time() - start_time
            sleep_time = max(0, LOOP_INTERVAL - elapsed_time)
            if sleep_time > 0:
                logging.debug(f"💤 次のループまで {sleep_time:.2f} 秒待機します。")
                await asyncio.sleep(sleep_time)
            else:
                 logging.warning(f"⚠️ メインループが遅延しています (実行時間: {elapsed_time:.2f} 秒)。")

        except Exception as e:
            # 致命的なエラーログとTelegram通知
            logging.critical(f"🚨 致命的なエラー: メインBOTループで例外が発生しました: {e}", exc_info=True)
            error_message = f"🚨 **致命的なエラー発生**\n\nメインBOTループの実行中にエラーが発生しました。\n\n**エラー:** <code>{str(e)[:500]}...</code>\n\n**BOTバージョン**: <code>{BOT_VERSION}</code>"
            try:
                 await send_telegram_notification(error_message)
                 logging.info(f"✅ 致命的エラー通知を送信しました (Ver: {BOT_VERSION})")
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

            # 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)


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
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "trade_cooldown_sec": TRADE_SIGNAL_COOLDOWN,
        "last_forex_fetch_time": LAST_FOREX_FETCH_TIME, # ★ 追加
        "forex_cache": FOREX_CACHE, # ★ 追加
    }
    return status_data


# FastAPI App & Main Execution
app = FastAPI(title="Apex BOT Trading System", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行されるタスク"""
    logging.info(f"🚀 FastAPI アプリケーション起動。バックグラウンドタスクを開始します。")
    
    # ★ 修正点 (Fix 4): CCXTクライアントの初期化を直接 await で実行
    await initialize_ccxt_client()
    
    # メインループをバックグラウンドで実行
    asyncio.create_task(main_bot_loop())
    # オープン注文監視ループをバックグラウンドで実行
    asyncio.create_task(open_order_management_loop())

if __name__ == "__main__":
    # ログ出力テストのためにボットバージョンを更新
    BOT_VERSION = "v19.0.53-p2 (Bugfix: Event Loop)"
    logging.info(f"💡 BOTバージョンを {BOT_VERSION} に設定しました。")
    # Renderの環境変数 $PORT を使用
    port = int(os.environ.get("PORT", 8000))
    # Uvicornの実行は同期処理であるため、メインプロセスでそのまま実行
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, reload=False) # main_render:app としてモジュール名とアプリ名を指定
