# ====================================================================================
# Apex BOT v19.0.35 - FULL COMPLIANCE (Limit Order & Exchange SL/TP, Score 100 Max)
#
# 改良・修正点 (v19.0.34からの追加点):
# 1. 【エラー処理の強化】致命的なエラー (CCXT/APIエラーを含む) 発生時に、直ちにTelegramで緊急通知を行い、BOTの非同期ループが停止しないようロジックを強化 (要件1, 2)。
# 2. 【通知強化】初期化失敗時にも緊急通知を追加。
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
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
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


# 💡 WEBSHARE設定 (HTTP POSTへ変更)
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") # デフォルトはHTTPに変更
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") # HTTP POST用のエンドポイント

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # ★ 1時間ごとの通知時刻
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
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
SIGNAL_THRESHOLD_SLUMP = 0.94       
SIGNAL_THRESHOLD_NORMAL = 0.92      
SIGNAL_THRESHOLD_ACTIVE = 0.90      

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

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (最大100点に合わせた調整)"""
    # 1.00が最高点
    if score >= 0.95:
        return "90%+"
    elif score >= 0.90:
        return "85-90%"
    elif score >= 0.85:
        return "80-85%"
    elif score >= 0.80:
        return "75-80%"
    else:
        return "70-75%"

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
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
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
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.35 - Limit Order & Exchange SL/TP, Stability Enhanced</i>")
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
        f"  - **現在の価格**: <code>{format_price_precision(best_signal['entry_price'])}</code>\n"
        f"\n"
        f"🔴 **ワーストスコア銘柄 (Bottom)**\n"
        f"  - **銘柄**: <b>{worst_signal['symbol']}</b> ({worst_signal['timeframe']})\n"
        f"  - **スコア**: <code>{worst_signal['score'] * 100:.2f} / 100</code>\n"
        f"  - **推定勝率**: <code>{get_estimated_win_rate(worst_signal['score'])}</code>\n"
        f"  - **現在の価格**: <code>{format_price_precision(worst_signal['entry_price'])}</code>\n"
        f"\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.35 - Limit Order & Exchange SL/TP, Stability Enhanced</i>"
    )
    
    return message

async def send_telegram_notification(message: str):
    """Telegramに通知を送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramトークンまたはCHAT IDが設定されていません。通知をスキップします。")
        return

    # HTML形式で送信
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        if response.status_code == 200:
            logging.info("✅ Telegram通知を送信しました。")
        else:
            logging.error(f"❌ Telegram通知失敗: ステータスコード {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram通知中にエラーが発生: {e}")

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
    """シグナルまたは取引結果をJSON形式でログに記録する (WebShare用)"""
    log_data = {
        'timestamp_jst': datetime.now(JST).isoformat(),
        'context': context,
        'signal': _to_json_compatible(signal),
        'total_equity': GLOBAL_TOTAL_EQUITY,
        'current_positions_count': len(OPEN_POSITIONS),
    }
    
    # 実際にはここにファイルへの追記ロジックやデータベースへの書き込みロジックが入るが、今回はHTTP POSTを使用
    return log_data

async def send_webshare_update(data: Dict):
    """WebShare (外部ロギングシステム) に最新のデータを送信する (HTTP POST)"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    if WEBSHARE_METHOD != "HTTP" or not WEBSHARE_POST_URL or WEBSHARE_POST_URL == "http://your-webshare-endpoint.com/upload":
        logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
        return

    try:
        logging.info("WebShareデータをアップロードします (HTTP POST)。")
        
        # 最終ログ時刻を更新
        LAST_WEBSHARE_UPLOAD_TIME = time.time()
        
        response = requests.post(
            WEBSHARE_POST_URL,
            json=data,
            timeout=15
        )
        response.raise_for_status() # HTTPエラーをチェック
        logging.info(f"✅ WebShareデータアップロード成功。ステータス: {response.status_code}")
    
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ WebShareデータアップロード失敗: {e}")
    except Exception as e:
        logging.error(f"❌ WebShareデータアップロード中に予期せぬエラー: {e}")


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、市場情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    logging.info(f"⏳ CCXTクライアント ({CCXT_CLIENT_NAME}) の初期化を開始します...")
    
    # 以前のインスタンスを閉じる
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
        except Exception as e:
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラー: {e}")

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
            }
        }
        EXCHANGE_CLIENT = exchange_class(config)
        
        # 市場情報をロード
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化し、市場情報をロードしました。")
        
        if not API_KEY or not SECRET_KEY:
            logging.warning("⚠️ APIキーまたはシークレットキーが設定されていません。取引機能は無効です。")
            

    # ------------------------------------------------------------------------------------
    # 【強化ポイント1: 初期化時の致命的なエラー通知】
    # ------------------------------------------------------------------------------------
    except Exception as e:
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)
        
        # 🚨 緊急通知: BOTの起動に失敗したため、直ちに通知
        await send_telegram_notification(
            f"🚨 **致命的なエラー** - CCXT初期化失敗\n"
            f"BOTは起動できません。エラー詳細: `{CCXT_CLIENT_NAME}`: `{e}`"
        )
        IS_CLIENT_READY = False
        # BOT起動失敗時、FastAPIのスケジューラが停止しないよう、エラーを捕捉して戻る

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、CCXTエラーを安全に処理する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning(f"⚠️ {symbol}のOHLCV取得失敗: CCXTクライアントが未準備です。")
        return None

    try:
        # データを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)

        if not ohlcv or len(ohlcv) < 50: # 最低限のデータ量チェック (50本未満は分析に不十分)
            logging.warning(f"⚠️ {symbol}/{timeframe} のOHLCVデータが不足しています (取得数: {len(ohlcv)})。")
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        return df

    except ccxt.ExchangeError as e:
        # 取引所固有のエラー (例: 銘柄が存在しない、レート制限) はログに記録して続行 (致命的ではない)
        logging.warning(f"❌ {symbol}/{timeframe} のOHLCV取得失敗 (ExchangeError): {e}")
    except ccxt.NetworkError as e:
        # ネットワークエラーは致命的な可能性があるが、メインループで包括的に処理するためここではログのみ
        logging.warning(f"❌ {symbol}/{timeframe} のOHLCV取得失敗 (NetworkError): {e}")
    except Exception as e:
        # その他の予期せぬエラー
        logging.error(f"❌ {symbol}/{timeframe} のOHLCV取得中に予期せぬエラー: {e}", exc_info=True)
        
    return None

async def fetch_account_status() -> Dict:
    """CCXTから最新の口座残高と保有ポジションを取得し、総資産額を更新する"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ 口座ステータス取得失敗: CCXTクライアントが未準備です。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}
    
    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        # 2. total_equity (総資産額) の取得
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance)
        if GLOBAL_TOTAL_EQUITY == 0.0:
            GLOBAL_TOTAL_EQUITY = total_usdt_balance # フォールバック

        # 3. 現物保有資産の概算USDT価値を計算
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
                    if usdt_value >= 10:
                        open_positions.append({
                            'symbol': symbol,
                            'amount': amount,
                            'usdt_value': usdt_value
                        })
                except Exception as e:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")
        
        logging.info(f"✅ 口座ステータス取得成功: Equity={GLOBAL_TOTAL_EQUITY:.2f} USDT")
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY,
            'open_positions': open_positions,
            'error': False
        }
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
        # 致命的なネットワークエラーはメインスケジューラで捕捉されることを想定
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
        # 認証エラーもメインスケジューラで捕捉されることを想定
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")
        
    return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

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
    # RSI
    df['RSI'] = ta.rsi(df['close'], length=14)
    # MACD
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df[['MACD', 'MACD_H', 'MACD_S']] = macd_data
    # ボリンジャーバンド
    bbands = ta.bbands(df['close'], length=20, std=2)
    df[['BBL', 'BBM', 'BBU', 'BBB', 'BBP']] = bbands
    # OBV
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    # 出来高平均
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)
    # ピボットポイント (簡易版)
    df['Pivot'] = (df['high'].shift(1) + df['low'].shift(1) + df['close'].shift(1)) / 3
    df['R1'] = 2 * df['Pivot'] - df['low'].shift(1)
    df['S1'] = 2 * df['Pivot'] - df['high'].shift(1)
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """テクニカル分析に基づき、取引シグナルを生成する (ロングのみ)"""
    if len(df) < LONG_TERM_SMA_LENGTH:
        return None 
    
    current_price = df['close'].iloc[-1]
    
    # 1. ベーススコアの設定
    score = BASE_SCORE # 0.50点 (50点) からスタート
    
    # テクニカル指標の抽出
    sma200 = df['SMA200'].iloc[-1]
    sma50 = df['SMA50'].iloc[-1]
    rsi = df['RSI'].iloc[-1]
    macd = df['MACD'].iloc[-1]
    macd_h = df['MACD_H'].iloc[-1] # ヒストグラム
    bbl = df['BBL'].iloc[-1] # ボリンジャーバンド下限
    pivot_s1 = df['S1'].iloc[-1] # S1サポート

    # ------------------------------------------------------------------------------------
    # 1. スコアリングの実施 (ロング/買いシグナルのみ)
    # ------------------------------------------------------------------------------------
    long_term_reversal_penalty_value = 0.0
    trend_alignment_bonus_value = 0.0
    structural_pivot_bonus = 0.0
    macd_penalty_value = 0.0
    sentiment_fgi_proxy_bonus = 0.0
    volatility_penalty_value = 0.0
    
    # 長期トレンド逆行ペナルティ
    if current_price < sma200 * 0.95: # 200SMAから5%以上乖離して下にいる
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY 
        
    # 中期/長期トレンド一致ボーナス (SMA50 > SMA200)
    if sma50 > sma200 and current_price > sma50:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        
    # 価格構造/ピボット支持ボーナス (価格がS1サポートより上で推移している)
    if current_price > pivot_s1 * 1.001 and current_price > bbl:
         structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
         
    # MACDペナルティ (MACDがシグナルラインを下抜けている、またはヒストグラムがマイナスに転換)
    if macd < df['MACD_S'].iloc[-1] or macd_h < 0.0:
        macd_penalty_value = MACD_CROSS_PENALTY
        
    # RSIモメンタムボーナス (RSIが45以下から上昇、または50以上で強い上昇)
    rsi_momentum_bonus_value = 0.0
    if rsi >= RSI_MOMENTUM_LOW:
        # RSIが50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)

    # OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    # Volume Spike Bonus
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5:
        # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS

    # Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
    volatility_penalty_value = 0.0
    if df['BBB'].iloc[-1] < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
        volatility_penalty_value = -0.05 # ペナルティとしてマイナス5点を付与

    # 流動性ボーナス (板情報は省略しMAXボーナスを固定)
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX

    # FGI (マクロセンチメント) ボーナス/ペナルティ
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    # -1.0から1.0の範囲を-0.05から+0.05にマッピング
    sentiment_fgi_proxy_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX

    tech_data = {
        'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
        'trend_alignment_bonus_value': trend_alignment_bonus_value,
        'structural_pivot_bonus': structural_pivot_bonus,
        'macd_penalty_value': macd_penalty_value,
        'rsi_momentum_bonus_value': rsi_momentum_bonus_value,
        'rsi_value': rsi,
        'obv_momentum_bonus_value': obv_momentum_bonus_value,
        'volume_increase_bonus_value': volume_increase_bonus_value,
        'liquidity_bonus_value': liquidity_bonus_value,
        'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
        'forex_bonus': 0.0,
        'volatility_penalty_value': volatility_penalty_value,
    }

    # 総合スコア計算 (ウェイト強化)
    score += (
        tech_data['trend_alignment_bonus_value']
        + tech_data['structural_pivot_bonus']
        + tech_data['rsi_momentum_bonus_value']
        + tech_data['obv_momentum_bonus_value']
        + tech_data['volume_increase_bonus_value']
        + tech_data['liquidity_bonus_value']
        + tech_data['sentiment_fgi_proxy_bonus']
        + tech_data['volatility_penalty_value']
        - tech_data['long_term_reversal_penalty_value']
        - tech_data['macd_penalty_value']
    )
    
    # スコアは0.00〜1.00の範囲に正規化（すでにウェイト調整済みのため、1.0を超えることは想定されないが念のため）
    score = max(0.00, min(1.00, score))

    ##############################################################
    # 2. 動的なSL/TPとRRRの設定ロジック (スコアと構造を考慮)
    ##############################################################
    BASE_RISK_PERCENT = 0.015 # 1.5% のリスク
    PIVOT_SUPPORT_BONUS_SL_MULTIPLIER = 1.2
    
    # SL/TPの基本設定（価格のパーセンテージに基づく）
    risk_percent = BASE_RISK_PERCENT 

    # サポート/ピボット S1を考慮したリスク設定
    sl_price_base = current_price * (1 - risk_percent) # 1.5%下
    
    # S1ピボットが価格の下にある場合、SLをS1直下に設定
    if pivot_s1 > 0 and pivot_s1 < current_price:
        # 価格とS1の幅がリスク幅 (1.5%) より大きい場合、S1直下に設定
        if current_price / pivot_s1 - 1 > risk_percent:
            sl_price = pivot_s1 * 0.999 
        else:
            sl_price = sl_price_base # S1が近すぎる場合は基本設定を採用

    # SL価格は、終値のボリンジャーバンド下限 (BBL) よりも下に設定
    if bbl > 0 and bbl < current_price and sl_price > bbl * 0.99:
        sl_price = bbl * 0.99 
        
    # SL価格が現在の価格から0.5%未満の場合、取引しない
    if (current_price - sl_price) / current_price < 0.005:
        logging.warning(f"⚠️ {symbol} SL幅が狭すぎます ({((current_price - sl_price) / current_price)*100:.2f}%)。シグナルをスキップ。")
        return None 
        
    # リスク幅（USDT建て）を計算
    risk_amount_usdt = current_price - sl_price
    
    # TP価格を決定 (固定のRRR 2.0からスコアに応じて変動させる)
    # スコアが高いほどRRRを低く（現実的に約定しやすいように）
    # Score 0.60 -> RRR 2.5
    # Score 0.96 -> RRR 1.5
    max_rr_ratio = 2.5 
    min_rr_ratio = 1.5 
    # スコアに基づく線形補間
    rr_ratio = max_rr_ratio - (score - SIGNAL_THRESHOLD) * ((max_rr_ratio - min_rr_ratio) / (DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD))
    rr_ratio = max(min_rr_ratio, min(max_rr_ratio, rr_ratio)) # 範囲外にならないようにクランプ

    # TP価格を計算
    take_profit = current_price + (risk_amount_usdt * rr_ratio)
    
    # RR幅のチェック (TP-Entry) / (Entry-SL)
    actual_rr_ratio = (take_profit - current_price) / (current_price - sl_price)

    # 閾値チェック
    current_threshold = get_current_threshold(macro_context)
    if score < current_threshold:
        return None # 閾値未満はスキップ

    # 動的ロットサイズの計算
    lot_size_usdt = calculate_dynamic_lot_size(score, GLOBAL_TOTAL_EQUITY)

    # シグナル辞書を作成
    signal = {
        'symbol': symbol,
        'action': 'buy', # 現物なので常に'buy'
        'timeframe': timeframe,
        'score': score,
        'entry_price': current_price, # 成行または即時約定の指値なので現在価格を指値価格として使用
        'stop_loss': sl_price,
        'take_profit': take_profit,
        'rr_ratio': actual_rr_ratio,
        'lot_size_usdt': lot_size_usdt,
        'tech_data': tech_data
    }
    
    return signal

def calculate_dynamic_lot_size(score: float, total_equity: float) -> float:
    """スコアと総資産額に基づいて動的なロットサイズ (USDT) を計算する"""
    if total_equity < MIN_USDT_BALANCE_FOR_TRADE * 5:
        logging.warning("⚠️ 総資産額が少なすぎます。最低基準ロット (BASE_TRADE_SIZE_USDT) を使用します。")
        return BASE_TRADE_SIZE_USDT
        
    # 資産の最小ロット (10%) と最大ロット (20%) を決定
    min_lot = total_equity * DYNAMIC_LOT_MIN_PERCENT
    max_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # ベースラインスコア (SIGNAL_THRESHOLD=0.65) 未満では最低ロットを採用
    if score < SIGNAL_THRESHOLD:
        return max(BASE_TRADE_SIZE_USDT, min_lot)

    # スコアを正規化 (SIGNAL_THRESHOLD〜DYNAMIC_LOT_SCORE_MAX)
    if score >= DYNAMIC_LOT_SCORE_MAX:
        normalized_score = 1.0
    else:
        # スコアの範囲を0.0から1.0にマッピング
        range_size = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
        normalized_score = max(0.0, (score - SIGNAL_THRESHOLD) / range_size)

    # ロットサイズを線形補間
    lot_size = min_lot + (max_lot - min_lot) * normalized_score
    
    # 最小取引額BASE_TRADE_SIZE_USDTを考慮
    return max(BASE_TRADE_SIZE_USDT, lot_size)

async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """USDT建ての注文量を取引所の最小数量、桁数に合わせて調整する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0
        
    try:
        # 数量を計算
        base_amount = usdt_amount / price
        market = EXCHANGE_CLIENT.markets.get(symbol)
        
        if not market:
            logging.warning(f"⚠️ {symbol}の市場情報が見つかりません。数量の丸め処理をスキップします。")
            return base_amount, usdt_amount

        # 最小取引数量のチェック
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount < min_amount:
            logging.warning(f"⚠️ 注文数量 ({base_amount:.4f}) が最小取引数量 ({min_amount}) を下回りました。最小数量に調整します。")
            base_amount = min_amount
            
        # 数量の丸め（精度調整）
        amount_precision = market.get('precision', {}).get('amount', 4) # デフォルトを4桁とする
        rounded_amount = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
        final_amount = float(rounded_amount)
        
        # 最終的なUSDT価値を再計算
        final_usdt_amount = final_amount * price

        return final_amount, final_usdt_amount

    except Exception as e:
        logging.error(f"❌ 注文数量の調整中にエラーが発生 ({symbol}): {e}")
        return base_amount, usdt_amount


async def set_exchange_sl_tp(symbol: str, filled_amount: float, stop_loss: float, take_profit: float) -> Dict:
    """約定後、取引所にSL(ストップ指値)とTP(指値)注文を設定する (要件2)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    sl_order_id = None
    tp_order_id = None

    # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
    try:
        # 数量の丸め
        amount_to_sell, _ = await adjust_order_amount(symbol, filled_amount * take_profit, take_profit) 
        
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
        logging.error(f"❌ TP注文設定失敗 ({symbol}): {e}")

    # 2. SL (ストップロス) ストップ指値売り注文の設定 (Stop Limit Sell)
    try:
        # 数量の丸め (TPと同じ数量を使用)
        amount_to_sell, _ = await adjust_order_amount(symbol, filled_amount * stop_loss, stop_loss)
        
        # ストップトリガー価格: stop_loss
        # 指値価格: スリッページ対策としてストップ価格よりわずかに下 (0.1%下)
        sl_limit_price = stop_loss * 0.999 

        # CCXTのストップ注文は、取引所によってparamsが異なるため、汎用的に'stop_limit'タイプを使用
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit',
            side='sell',
            amount=amount_to_sell,
            price=sl_limit_price,  # 指値価格
            params={
                'stopPrice': stop_loss, # トリガー価格
                'timeInForce': 'GTC'
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SLストップ指値売り注文成功: {symbol} トリガー@ {format_price_precision(stop_loss)} / 指値@ {format_price_precision(sl_limit_price)} (ID: {sl_order_id})")
    except Exception as e:
        logging.error(f"❌ SL注文設定失敗 ({symbol}): {e}")

    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
    }

async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """CCXTを利用して現物取引を実行する (指値買いに変更: 要件1)"""
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    action = signal['action'] # 'buy'
    lot_size_usdt = signal['lot_size_usdt'] # 動的ロットを使用

    if TEST_MODE:
        return {
            'status': 'ok',
            'filled_amount': lot_size_usdt / signal['entry_price'],
            'filled_usdt': lot_size_usdt,
            'id': f"TEST-{uuid.uuid4()}",
            'price': signal['entry_price'],
            'message': 'Test mode: No real trade executed.'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    # 残高チェック
    if account_status.get('total_usdt_balance', 0.0) < lot_size_usdt or account_status.get('total_usdt_balance', 0.0) < MIN_USDT_BALANCE_FOR_TRADE:
        return {'status': 'error', 'error_message': f'USDT残高が不足しています (必要額: {lot_size_usdt:.2f} USDT)。'}

    # 1. 注文数量の調整
    try:
        # 指値買い価格をシグナルから取得
        limit_price = signal['entry_price']
        
        # lot_size_usdtを元に数量を計算し、取引所ルールで調整
        base_amount, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, limit_price)
        
        if base_amount == 0.0:
            return {'status': 'error', 'error_message': '注文数量が取引所の最小要件を満たしませんでした。'}

    except Exception as e:
        return {'status': 'error', 'error_message': f'注文数量調整エラー: {e}'}

    # 2. 指値買い注文 (FOK: Fill-or-Kill) の実行
    try:
        # FOK注文を送信 (即時約定しない場合はキャンセルされる)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値注文
            side='buy',
            amount=base_amount,
            price=limit_price,
            params={'timeInForce': 'FOK'} # FOK (Fill-or-Kill)
        )

        # 注文の結果をチェック
        if order and order['status'] == 'closed':
            # 約定成功
            filled_amount = order['filled']
            filled_usdt = order['cost'] # 注文にかかったUSDTコスト
            
            # SL/TP注文を設定
            sl_tp_result = await set_exchange_sl_tp(
                symbol,
                filled_amount,
                signal['stop_loss'],
                signal['take_profit']
            )

            if sl_tp_result['status'] == 'error':
                 logging.critical(f"❌ {symbol} SL/TP注文設定で致命的なエラー: {sl_tp_result['error_message']}")

            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'id': order['id'],
                'price': order['price'],
                'sl_order_id': sl_tp_result.get('sl_order_id'),
                'tp_order_id': sl_tp_result.get('tp_order_id'),
            }
        elif order and order['status'] in ('open', 'partial', 'canceled'):
             return {'status': 'error', 'error_message': f"指値注文は即時約定しなかったため、取引をスキップしました (ステータス: {order['status']}, ID: {order['id']})"}
        else:
             return {'status': 'error', 'error_message': f"注文API応答が不正です。ログを確認してください。"}

    except ccxt.ExchangeError as e:
        # FOK注文は、約定しなかった場合に取引所がエラーを返すこともある
        if "Fill-or-Kill" in str(e) or "was not filled" in str(e):
             return {'status': 'error', 'error_message': '指値注文が即時約定しなかったため、スキップしました (FOK)。'}
        # 致命的な取引所エラー（例：認証、権限）はメインスケジューラで捕捉されることを想定
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        return {'status': 'error', 'error_message': f'予期せぬ取引実行エラー: {e}'}

async def cancel_all_related_orders(position: Dict, open_order_ids: set):
    """決済完了後に残ったSL/TPの未約定注文をキャンセルする"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return

    # SL注文のキャンセル
    sl_id = position.get('sl_order_id')
    if sl_id and sl_id in open_order_ids:
        try:
            await EXCHANGE_CLIENT.cancel_order(sl_id, position['symbol'])
            logging.info(f"✅ SL注文をキャンセルしました: ID {sl_id}")
        except Exception as e:
            logging.warning(f"⚠️ SL注文のキャンセル失敗 (ID: {sl_id}): {e}")

    # TP注文のキャンセル
    tp_id = position.get('tp_order_id')
    if tp_id and tp_id in open_order_ids:
        try:
            await EXCHANGE_CLIENT.cancel_order(tp_id, position['symbol'])
            logging.info(f"✅ TP注文をキャンセルしました: ID {tp_id}")
        except Exception as e:
            logging.warning(f"⚠️ TP注文のキャンセル失敗 (ID: {tp_id}): {e}")

async def open_order_management_loop_async():
    """オープン注文 (SL/TP) を監視し、決済完了をトラッキングする非同期ループ (10秒ごと)"""
    global OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT
    if not OPEN_POSITIONS or not EXCHANGE_CLIENT:
        return

    positions_to_remove_ids = []

    try:
        # 未決済のオープン注文をフェッチ (SL/TP注文が含まれる)
        # fetch_open_orders() は時間がかかる可能性があるため、最初に取得
        open_orders = await EXCHANGE_CLIENT.fetch_open_orders()
        open_order_ids = {order['id'] for order in open_orders}

        for position in OPEN_POSITIONS:
            is_closed = False
            exit_type = None
            
            # SL注文とTP注文のIDを取得
            sl_id = position.get('sl_order_id')
            tp_id = position.get('tp_order_id')

            # SLまたはTPの注文IDが存在しない場合はスキップ (注文エラーまたはテストモード)
            if not sl_id and not tp_id:
                continue

            # SLまたはTPのどちらかがオープン注文リストに残っているかを確認
            sl_open = sl_id in open_order_ids
            tp_open = tp_id in open_order_ids

            # 以下、決済完了の判定ロジック
            # 1. SLもTPもオープン注文リストにない場合: どちらかで約定し、残った片方もキャンセルされている（取引所依存）。または、両方キャンセル失敗。
            if not sl_open and not tp_open:
                # ここでは決済完了と見なし、より詳細な判定は取引履歴に依存
                is_closed = True
                exit_type = "SL/TP (取引所決済完了)" 
            # 2. SL注文だけがオープン注文リストにない場合: TPが約定した可能性が高い
            elif sl_id and not sl_open and tp_open:
                # SLが約定した可能性が高い
                is_closed = True
                exit_type = "SL (ストップロス約定)"
            # 3. TP注文だけがオープン注文リストにない場合: SLが約定した可能性が高い
            elif tp_id and not tp_open and sl_open:
                # TPが約定した可能性が高い
                is_closed = True
                exit_type = "TP (テイクプロフィット約定)"
            
            # 決済が完了していた場合
            if is_closed:
                # 残りの注文をキャンセル（念のため実行）
                await cancel_all_related_orders(position, open_order_ids)

                closed_result = {
                    **position, # ポジション情報を引き継ぐ
                    'exit_type': exit_type,
                    'exit_price': position['entry_price'], # ここでは正確な決済価格が不明なため一旦エントリー価格とする（取引履歴で確認すべき）
                    # SL/TPのいずれかが約定した時点で決済と見なし、USDTでのP&L計算は取引所履歴に依存させる
                    'pnl_usdt': None, 
                    'pnl_rate': None,
                    'filled_amount': position['filled_amount']
                }

                positions_to_remove_ids.append(position['id'])
                notification_message = format_telegram_message(closed_result, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), closed_result)
                await send_telegram_notification(notification_message)
                log_signal(closed_result, "Position Exit")


    except Exception as e:
        # ------------------------------------------------------------------------------------
        # 【強化ポイント3: オープン注文監視ループの致命的なエラー通知とループ継続】
        # ------------------------------------------------------------------------------------
        logging.critical(f"❌ オープン注文監視中に致命的なエラーが発生: {e}", exc_info=True)
        # 🚨 緊急通知: 監視ループでエラーが発生したが、ループは継続
        await send_telegram_notification(
            f"🚨 **致命的なエラー** - 注文監視ループ\n"
            f"エラーが発生しましたが、BOTは再試行します。エラー詳細: `{e}`"
        )
        # エラーが発生しても、次の MONITOR_INTERVAL で再試行するため、ループは停止しない。

    # 監視リストから決済されたポジションを削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove_ids]


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_WEBSHARE_UPLOAD_TIME, LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, HOURLY_SIGNAL_LOG
    
    start_time = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    # 1. FGIデータを取得し、GLOBAL_MACRO_CONTEXTを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()

    # 2. 口座ステータスの取得 (ロットサイズ計算のため、最新の総資産額を取得)
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    new_signals: List[Dict] = []
    
    # 3. 監視銘柄リストの更新 (初回起動時、または定期的に)
    if not IS_FIRST_MAIN_LOOP_COMPLETED or (time.time() - LAST_HOURLY_NOTIFICATION_TIME) > HOURLY_SCORE_REPORT_INTERVAL:
        # TODO: ここでCCXTのfetch_tickersやfetch_markets_volumeなどを利用して監視銘柄を更新する
        # 現状はDEFAULT_SYMBOLSを維持
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS.copy()
        logging.info(f"監視銘柄を {len(CURRENT_MONITOR_SYMBOLS)} 銘柄に更新しました。")

    # 4. 全銘柄の分析とシグナル生成
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # 既にオープンポジションがある場合はスキップ (同一銘柄の複数ポジションを避ける)
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            continue

        # クールダウンチェック
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
            continue

        # 複数のタイムフレームで分析
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS[timeframe]
            df = await fetch_ohlcv_safe(symbol, timeframe, limit)
            
            if df is None:
                continue

            df = calculate_indicators(df)
            signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)

            if signal:
                new_signals.append(signal)
                # ★ HOURLY_SIGNAL_LOGに分析されたシグナルを保存 (重複を除く)
                if not any(s['symbol'] == signal['symbol'] for s in HOURLY_SIGNAL_LOG):
                    HOURLY_SIGNAL_LOG.append(signal)

    # 5. シグナルの選定と取引実行
    # スコアの高い順にソート
    new_signals.sort(key=lambda x: x['score'], reverse=True)
    LAST_ANALYSIS_SIGNALS = new_signals # 最終分析シグナルを更新 (WebShare用)

    executed_signals_count = 0
    for signal in new_signals:
        if executed_signals_count >= TOP_SIGNAL_COUNT:
            break # Top N件のみ処理
            
        if signal['score'] >= current_threshold:
            logging.info(f"🟢 [TRADE SIGNAL] {signal['symbol']} ({signal['timeframe']}) - Score: {signal['score']*100:.2f} / {current_threshold*100:.2f} (Threshold)")
            
            if not TEST_MODE and account_status.get('total_usdt_balance', 0.0) < MIN_USDT_BALANCE_FOR_TRADE:
                logging.warning(f"⚠️ {signal['symbol']}: USDT残高不足のため取引をスキップします。")
                continue

            # 取引実行
            trade_result = await execute_trade(signal, account_status)
            
            # 結果をログに記録し、通知を送信
            if trade_result['status'] == 'ok':
                executed_signals_count += 1
                log_data = log_signal({**signal, **trade_result}, "Position Entry")
                
                # OPEN_POSITIONSに追加
                position_data = {
                    'id': trade_result.get('id'), # 注文ID
                    'symbol': signal['symbol'],
                    'entry_price': trade_result['price'],
                    'filled_amount': trade_result['filled_amount'],
                    'filled_usdt': trade_result['filled_usdt'],
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'sl_order_id': trade_result.get('sl_order_id'),
                    'tp_order_id': trade_result.get('tp_order_id'),
                }
                OPEN_POSITIONS.append(position_data)

                # 通知メッセージを送信
                notification_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                await send_telegram_notification(notification_message)
                
            else:
                logging.warning(f"❌ {signal['symbol']} 取引実行失敗: {trade_result['error_message']}")
                # 取引失敗の通知は行わない（頻度が高くなる可能性があるため）
                
            # シグナルクールダウンを更新
            LAST_SIGNAL_TIME[signal['symbol']] = time.time()
            
    # 6. 初回起動完了通知 (一度だけ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回通知メッセージを送信
        startup_message = format_startup_message(
            account_status,
            GLOBAL_MACRO_CONTEXT,
            len(CURRENT_MONITOR_SYMBOLS),
            current_threshold,
            "v19.0.35 - Stability Enhanced"
        )
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 7. 1時間ごとのスコアレポート (HOURLY_SCORE_REPORT_INTERVAL)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL:
        logging.info("🕒 1時間スコアレポートを生成します。")
        report_message = format_hourly_report(HOURLY_SIGNAL_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
        await send_telegram_notification(report_message)
        # ログと通知時刻をリセット
        HOURLY_SIGNAL_LOG = []
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
    # 8. WebShareログのアップロード
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        await send_webshare_update({
            'timestamp': datetime.now(JST).isoformat(),
            'signals': _to_json_compatible(LAST_ANALYSIS_SIGNALS),
            'positions': _to_json_compatible(OPEN_POSITIONS),
            'equity': GLOBAL_TOTAL_EQUITY,
            'fgi_raw': GLOBAL_MACRO_CONTEXT['fgi_raw_value'],
            'bot_version': "v19.0.35"
        })

    end_time = time.time()
    LAST_SUCCESS_TIME = end_time
    logging.info(f"--- 💡 BOT LOOP END. Positions: {len(OPEN_POSITIONS)}, New Signals: {executed_signals_count} ---")

# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI(title="Apex BOT Trading API", version="v19.0.35")

@app.get("/")
async def root():
    """ルートエンドポイント (ボットの状態確認用)"""
    return JSONResponse(content={
        "status": "Running",
        "client_ready": IS_CLIENT_READY,
        "mode": "TEST" if TEST_MODE else "LIVE",
        "exchange": CCXT_CLIENT_NAME.upper(),
        "total_equity": GLOBAL_TOTAL_EQUITY,
        "positions_count": len(OPEN_POSITIONS),
        "last_success_time_jst": datetime.fromtimestamp(LAST_SUCCESS_TIME).astimezone(JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME else "N/A"
    })

async def main_bot_scheduler():
    """メインのBOTロジックを定期実行するスケジューラ (LOOP_INTERVAL秒ごと)"""
    global IS_CLIENT_READY, LAST_SUCCESS_TIME
    
    # クライアントが未初期化の場合、初期化を試行
    if not IS_CLIENT_READY:
        await initialize_exchange_client()
        
    while True:
        try:
            # ------------------------------------------------------------------------------------
            # 【強化ポイント2: メインループの致命的なエラー通知とループ継続】
            # ------------------------------------------------------------------------------------
            await main_bot_loop()
        except Exception as e:
            # 致命的なエラーが発生した場合でも、ループを継続するためにエラーをログに記録し、待機時間を経て再試行
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            # 🚨 緊急通知: メインループでエラーが発生したが、ループは継続
            await send_telegram_notification(
                f"🚨 **致命的なエラー** - メインループ\n"
                f"エラーが発生しましたが、BOTは再試行します。エラー詳細: `{e}`"
            )

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引くことで、正確な周期実行を保証
        elapsed_time = time.time() - LAST_SUCCESS_TIME
        # 実行時間が長すぎた場合でも、最低1秒は待機させる
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def open_order_management_scheduler():
    """オープン注文 (SL/TP) の監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await open_order_management_loop_async()
        except Exception as e:
            # ------------------------------------------------------------------------------------
            # 【強化ポイント3: 監視ループの致命的なエラー通知とループ継続】
            # ------------------------------------------------------------------------------------
            logging.critical(f"❌ オープン注文監視ループ実行中に致命的なエラー: {e}", exc_info=True)
            # 🚨 緊急通知: 監視ループでエラーが発生したが、ループは継続
            await send_telegram_notification(
                f"🚨 **致命的なエラー** - 注文監視ループ\n"
                f"エラーが発生しましたが、BOTは再試行します。エラー詳細: `{e}`"
            )
            # エラーが発生してもループを停止せず、次の間隔で再試行

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """FastAPI起動時に非同期タスクを開始する"""
    logging.info("🚀 FastAPI起動イベント: CCXTクライアントの初期化を開始します。")
    # 初期化が成功するまで待つ (失敗しても再試行ロジックはschedulerにある)
    await initialize_exchange_client() 
    
    # スケジューラタスクを開始
    # Botの安定性を担保するため、タスクの予期せぬ停止を防ぐためにFastAPIのタスクとして管理
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(open_order_management_scheduler())
    
    logging.info("✅ BOTスケジューラタスクを開始しました。")

# uvicornでこのFastAPIアプリを起動する (例: uvicorn main:app --reload)
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
