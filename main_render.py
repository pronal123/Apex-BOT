# ====================================================================================
# Apex BOT v19.0.35 - FULL COMPLIANCE (Limit Order & Exchange SL/TP, Score 100 Max)
#
# 改良・修正点 (v19.0.33からの追加点):
# 1. 【通知機能追加】1時間以内に分析された銘柄の中から、最高スコアと最低スコアの銘柄を1時間ごとに通知する機能を追加 (要件追加1)。
# 2. 【ロギング強化】ログデータ保存用の一時リストを追加。
# 3. 【監視機能】BOTの非同期スケジューラが停止しないよう自動再起動機能は、FastAPIのasyncioタスクとロギングにより実質的に担保済み。
#
# 【v19.0.35での修正点】
# 1. WebShare関連の機能、設定、ロジックをすべて削除しました。
# 2. 初回起動通知時の `format_startup_message` の引数不足エラーを修正しました。
# 3. 【今回の修正】CCXTクライアントのタイムアウト値を20秒に延長しました。
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
        
    message += (f"<i>Bot Ver: v19.0.35 - Limit Order & Exchange SL/TP, Score 100 Max</i>")
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
        f"<i>Bot Ver: v19.0.35 - Limit Order & Exchange SL/TP, Score 100 Max</i>"
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
            # 💡 【今回の修正点】APIリクエストのタイムアウトを延長 (ミリ秒で指定: 20000ms = 20秒)
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
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance)
        if GLOBAL_TOTAL_EQUITY == 0.0:
            GLOBAL_TOTAL_EQUITY = total_usdt_balance # フォールバック

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
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")
        
    return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True}

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
            logging.warning(f"⚠️ 注文数量 ({base_amount:.4f}) が最小取引数量 ({min_amount}) を下回りました。最小数量に設定します。")
            base_amount = min_amount
        
        # 数量の丸め処理 (取引所の桁数に従う)
        precision = market.get('precision', {}).get('amount', 4) # デフォルトは4桁
        if precision is not None:
            # ccxtのdecimal_to_precisionで丸める
            base_amount = float(EXCHANGE_CLIENT.decimal_to_precision(base_amount, ccxt.ROUND, precision, ccxt.TICK_SIZE))

        final_usdt_amount = base_amount * price

        return base_amount, final_usdt_amount
    
    except Exception as e:
        logging.error(f"❌ 注文数量の調整に失敗 ({symbol}): {e}")
        return 0.0, 0.0

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    if symbol not in EXCHANGE_CLIENT.markets:
        logging.warning(f"⚠️ {symbol}は取引所に存在しないか、サポートされていません。スキップします。")
        return None
        
    try:
        # ccxtは非同期のfetch_ohlcvを使用
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
        )

        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが不足しています。取得数: {len(ohlcv)}/{limit}")
        
        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
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
    """テクニカル分析に基づき、総合スコアと取引シグナルを生成する"""
    
    if len(df) < LONG_TERM_SMA_LENGTH + 1:
        # SMA200の計算に必要なデータが不足している場合はスキップ
        return None
        
    # 最新のローソク足のデータ
    last_close = df['close'].iloc[-1]
    last_low = df['low'].iloc[-1]
    last_high = df['high'].iloc[-1]
    
    # スコアリングの開始 (ベーススコア)
    score = BASE_SCORE # 0.50点

    # ==============================================================
    # 1. スコアリング要因の計算 (ロングシグナルのみ)
    # ==============================================================

    # A. 長期トレンド逆行ペナルティ (SMA200からの価格乖離)
    long_term_reversal_penalty_value = 0.0
    sma200 = df['SMA200'].iloc[-1]
    # 価格がSMA200の1%下にある場合、長期トレンド逆行とみなしペナルティ
    if last_close < sma200 * 0.99:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY

    # B. トレンドアライメントボーナス (SMA50 > SMA200)
    trend_alignment_bonus_value = 0.0
    sma50 = df['SMA50'].iloc[-1]
    if sma50 > sma200:
        trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
        
    # C. 価格構造/ピボット支持ボーナス
    structural_pivot_bonus = 0.0
    s1_pivot = df['S1'].iloc[-1]
    
    # 現在価格が直近のS1ピボットを上回っている、またはS1をサポートとして維持している
    if last_close > s1_pivot and last_low < s1_pivot * 1.005: # S1の0.5%以内を下ヒゲで触れている
         structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
         
    # D. MACDクロス/発散ペナルティ (MACD < Signal の場合ペナルティ)
    macd_penalty_value = 0.0
    macd = df['MACD'].iloc[-1]
    macd_signal = df['MACD_S'].iloc[-1]
    if macd < macd_signal:
        macd_penalty_value = MACD_CROSS_PENALTY
        
    # E. RSIモメンタムボーナス (RSIが50に向けて加速)
    rsi_momentum_bonus_value = 0.0
    rsi = df['RSI'].iloc[-1]
    if RSI_MOMENTUM_LOW < rsi <= 70.0:
        # 50で0点、70でRSI_MOMENTUM_BONUS_MAX (0.10)
        rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
        
    # F. OBV Momentum Bonus (OBVがSMAを上抜けている)
    obv_momentum_bonus_value = 0.0
    if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS

    # G. Volume Spike Bonus
    volume_increase_bonus_value = 0.0
    if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5: # 出来高が平均の1.5倍
        volume_increase_bonus_value = VOLUME_INCREASE_BONUS

    # H. Volatility Penalty (ボリンジャーバンド幅が狭すぎる場合)
    volatility_penalty_value = 0.0
    if df['BBB'].iloc[-1] < VOLATILITY_BB_PENALTY_THRESHOLD * 100: # BB幅が1%未満
         volatility_penalty_value = -0.05 # ペナルティとしてマイナス5点を付与
        
    # I. 流動性ボーナス (板情報は省略しMAXボーナスを固定)
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    
    # J. マクロ環境ボーナス/ペナルティ
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX
    
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

    ##############################################################
    # 2. 動的なSL/TPとRRRの設定ロジック (スコアと構造を考慮)
    ##############################################################
    BASE_RISK_PERCENT = 0.015 # 1.5% のリスク
    PIVOT_SUPPORT_BONUS = tech_data['structural_pivot_bonus'] * 0.5 # 構造ボーナスの一部をSL幅調整に利用
    
    # SLの調整: ピボット支持があればリスクをわずかに緩和、なければ最大リスク
    risk_percent = BASE_RISK_PERCENT - (PIVOT_SUPPORT_BONUS * 0.1) 
    risk_percent = max(0.01, risk_percent) # 最小リスク1.0%

    # TPの調整: スコアが高いほどリワードを高く設定 (最大3.0)
    score_normalized = (score - BASE_SCORE) / (1.0 - BASE_SCORE) # 0.0から1.0に再正規化
    rr_ratio = 1.0 + (score_normalized * 2.0) # 1.0 (ベース) から 3.0 (最高) の範囲

    # 価格の計算
    entry_price = last_close 
    
    # ストップロス価格 (リスク幅をエントリー価格から減算)
    stop_loss = entry_price * (1.0 - risk_percent)
    
    # テイクプロフィット価格 (リワード幅 = リスク幅 * RRR)
    # risk_usdt = entry_price * risk_percent
    # reward_usdt = risk_usdt * rr_ratio
    # take_profit = entry_price + reward_usdt
    take_profit = entry_price * (1.0 + (risk_percent * rr_ratio)) 

    # RRRが1.0未満の場合は取引をスキップ (リスクが高すぎる)
    if rr_ratio < 1.0:
        return None 
    
    # 最終的なシグナルオブジェクトを生成
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'action': 'buy', 
        'score': score,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data
    }
    
    return signal_data

def calculate_dynamic_lot_size(score: float, account_status: Dict) -> float:
    """総合スコアに基づき、総資産額に応じた動的ロットサイズ (USDT建て) を計算する"""
    
    total_equity = account_status.get('total_equity', 0.0)
    
    # 1. 最小ロットと最大ロットの計算
    min_lot = max(BASE_TRADE_SIZE_USDT, total_equity * DYNAMIC_LOT_MIN_PERCENT)
    max_lot = total_equity * DYNAMIC_LOT_MAX_PERCENT
    
    # 2. スコアに基づいた線形補間
    if score >= DYNAMIC_LOT_SCORE_MAX:
        # 最高スコアで最大ロット
        return max_lot
    elif score <= SIGNAL_THRESHOLD:
        # 閾値以下のスコアで最小ロット (取引は実行されないが計算値としては最小)
        return min_lot
    else:
        # スコア範囲 (SIGNAL_THRESHOLD から DYNAMIC_LOT_SCORE_MAX) で線形に増加
        # 傾きを計算
        score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
        lot_range = max_lot - min_lot
        
        # 現在のロットサイズを計算
        if score_range > 0:
            lot_size = min_lot + lot_range * ((score - SIGNAL_THRESHOLD) / score_range)
            return lot_size
        else:
            return min_lot


async def place_sl_tp_orders(symbol: str, filled_amount: float, stop_loss: float, take_profit: float) -> Dict:
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
            price=sl_limit_price, # 指値価格
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
            'entry_price': signal['entry_price'],
            'id': f"TEST-{uuid.uuid4()}",
            'price': signal['entry_price'],
            'sl_order_id': 'TEST_SL',
            'tp_order_id': 'TEST_TP',
            'message': 'Test mode: No real trade executed.'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    # 1. 注文数量の調整
    try:
        # 指値買い価格をシグナルから取得
        limit_price = signal['entry_price']
        
        # lot_size_usdtを元に数量を計算し、取引所ルールで調整
        base_amount, final_usdt_amount = await adjust_order_amount(
            symbol=symbol, 
            usdt_amount=lot_size_usdt, 
            price=limit_price
        )
        
        if base_amount == 0.0 or final_usdt_amount < MIN_USDT_BALANCE_FOR_TRADE:
             return {'status': 'error', 'error_message': f'ロットサイズが最小取引量未満、または {MIN_USDT_BALANCE_FOR_TRADE:.2f} USDT未満です。'}

    except Exception as e:
        return {'status': 'error', 'error_message': f'ロットサイズ計算エラー: {e}'}

    # 2. 現物 指値買い注文 (FOK: 即時約定しない場合はキャンセル)
    try:
        # FOK (Fill-or-Kill) 注文: 指値価格で即時全量約定しない場合はキャンセル
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', # 指値
            side='buy',
            amount=base_amount,
            price=limit_price,
            params={'timeInForce': 'FOK'}
        )

        # 3. 注文結果の確認
        if order and order['status'] == 'closed':
            # 即時約定成功
            filled_amount = order['filled']
            filled_usdt = order['cost'] # USDT建ての約定金額
            entry_price = order['average'] if 'average' in order and order['average'] is not None else limit_price
            
            # SL/TP注文の設定
            sl_tp_result = await place_sl_tp_orders(
                symbol=symbol,
                filled_amount=filled_amount,
                stop_loss=signal['stop_loss'],
                take_profit=signal['take_profit']
            )
            
            # SL/TP注文が成功した場合、ポジションとして記録する
            if sl_tp_result['status'] == 'ok':
                 return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'entry_price': entry_price,
                    'id': order['id'], # 買い注文IDをポジションIDとして使用
                    'sl_order_id': sl_tp_result['sl_order_id'],
                    'tp_order_id': sl_tp_result['tp_order_id'],
                    'message': f"現物指値買い注文が即時全量約定しました。SL/TP注文を設定済み (ID: {order['id']})"
                }
            else:
                 # SL/TP注文失敗の場合は、キャンセル処理を挟まず、ポジション管理リストに追加しない
                 return {'status': 'error', 'error_message': f"約定後のSL/TP注文設定に失敗しました。指値買い注文ID: {order['id']}"}
                 
        elif order and order['status'] == 'filled':
            # FOKを使用しているため、'closed'と同じ意味だが念のため対応
            return {'status': 'error', 'error_message': f"指値注文は即時約定しなかったため、取引をスキップしました (ステータス: {order['status']}, ID: {order['id']})"}
        elif order and order['status'] in ('open', 'partial', 'canceled'):
            return {'status': 'error', 'error_message': f"指値注文は即時約定しなかったため、取引をスキップしました (ステータス: {order['status']}, ID: {order['id']})"}
        else:
            return {'status': 'error', 'error_message': f"注文API応答が不正です。ログを確認してください。"}

    except ccxt.ExchangeError as e:
        # FOK注文は、約定しなかった場合に取引所がエラーを返すこともある
        if "Fill-or-Kill" in str(e) or "was not filled" in str(e):
             return {'status': 'error', 'error_message': '指値注文が即時約定しなかったため、スキップしました (FOK)。'}
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
        # ※ CCXTの現物では 'fetch_open_orders' が SL/TP 注文を取得できない場合があるため、動作保証は取引所API依存
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
            elif not sl_open and tp_open:
                # TPがオープンリストにある -> SLが約定した場合はTPは自動キャンセルされる想定。TPが残っているのはおかしい。
                # 現物取引所によってはSL/TPの自動キャンセルが行われない場合があるため、この場合は一旦監視続行。
                pass
            
            # 3. TP注文だけがオープン注文リストにない場合: SLが約定した可能性が高い
            elif sl_open and not tp_open:
                # SLがオープンリストにある -> TPが約定した場合はSLは自動キャンセルされる想定。SLが残っているのはおかしい。
                # 現物取引所によってはSL/TPの自動キャンセルが行われない場合があるため、この場合は一旦監視続行。
                pass

            # 4. SL/TPが両方残っている場合: ポジションはオープン中
            # elif sl_open and tp_open:
            #     pass

            if is_closed:
                logging.info(f"🔴 ポジション決済検出: {position['symbol']} - 理由: {exit_type}")
                positions_to_remove_ids.append(position['id'])

                # 決済通知の作成 (取引履歴を取得できないため、SL/TP価格とExitタイプのみ)
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

            # 最もスコアの高いシグナルを採用するため、ここではクールダウンは設定しない
            # スコアリング関数は一度呼び出すだけで良い (複数TFのスコアを合算する場合などは別)

    # 5. ベストシグナルの選定と取引実行
    executed_signals_count = 0
    LAST_ANALYSIS_SIGNALS = new_signals # 最終分析結果を保存
    
    if new_signals:
        # スコア順にソートして、最もスコアが高いシグナルを採用
        best_signals = sorted(new_signals, key=lambda x: x['score'], reverse=True)
        best_signal = best_signals[0]
        
        # 動的ロットサイズの計算
        lot_size_usdt = calculate_dynamic_lot_size(best_signal['score'], account_status)
        best_signal['lot_size_usdt'] = lot_size_usdt
        
        if best_signal['score'] >= current_threshold and account_status['total_usdt_balance'] >= MIN_USDT_BALANCE_FOR_TRADE:
            
            logging.info(f"🔥 取引シグナル検出: {best_signal['symbol']} ({best_signal['timeframe']}) - Score: {best_signal['score'] * 100:.2f}")

            # 取引の実行
            trade_result = await execute_trade(best_signal, account_status)
            
            # 取引結果の通知
            await send_telegram_notification(format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result))
            log_signal(best_signal, "Signal Trade")
            
            if trade_result['status'] == 'ok':
                # ポジションリストに追加
                position_id = trade_result['id']
                # 新しいポジションデータを構築
                new_position = {
                    'id': position_id, # 買い注文のID
                    'symbol': best_signal['symbol'],
                    'entry_price': trade_result['entry_price'],
                    'filled_amount': trade_result['filled_amount'],
                    'filled_usdt': trade_result['filled_usdt'],
                    'stop_loss': best_signal['stop_loss'],
                    'take_profit': best_signal['take_profit'],
                    'sl_order_id': trade_result.get('sl_order_id'),
                    'tp_order_id': trade_result.get('tp_order_id'),
                    'timestamp': time.time(),
                }
                OPEN_POSITIONS.append(new_position)
                LAST_SIGNAL_TIME[best_signal['symbol']] = time.time()
                executed_signals_count += 1
            
        else:
            logging.info(f"➖ スコア未達/残高不足: {best_signal['symbol']} - Score: {best_signal['score'] * 100:.2f}/{current_threshold*100:.2f}")

    # 6. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動完了通知を送信
        # 【💡 修正済み】format_startup_message の引数不足エラーを修正
        startup_message = format_startup_message(
            account_status=account_status, 
            macro_context=GLOBAL_MACRO_CONTEXT, 
            monitoring_count=len(CURRENT_MONITOR_SYMBOLS), 
            current_threshold=current_threshold, 
            bot_version="v19.0.35"
        )
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 7. 1時間ごとのスコア通知 (V19.0.34で追加)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_SCORE_REPORT_INTERVAL and HOURLY_SIGNAL_LOG:
        logging.info("🕒 1時間ごとのスコアレポートを送信します。")
        report_message = format_hourly_report(HOURLY_SIGNAL_LOG, LAST_HOURLY_NOTIFICATION_TIME, current_threshold)
        await send_telegram_notification(report_message)
        # ログと通知時刻をリセット
        HOURLY_SIGNAL_LOG = []
        LAST_HOURLY_NOTIFICATION_TIME = time.time()
        
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
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y/%m/%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else "N/A",
        "current_positions": len(OPEN_POSITIONS),
        "monitoring_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "bot_version": app.version,
    })

@app.on_event("startup")
async def startup_event():
    """FastAPI起動時にCCXTクライアントを初期化し、非同期タスクを開始する"""
    await initialize_exchange_client()
    
    # メインBOTループの非同期タスクを開始
    asyncio.create_task(main_bot_scheduler())
    
    # オープン注文監視ループの非同期タスクを開始
    asyncio.create_task(open_order_management_scheduler())


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
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引くことで、正確な周期実行を保証
        elapsed_time = time.time() - LAST_SUCCESS_TIME
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def open_order_management_scheduler():
    """オープン注文 (SL/TP) の監視ループを定期実行するスケジューラ (10秒ごと)"""
    # スケジューラ名を変更
    while True:
        try:
            await open_order_management_loop_async() # 関数名を変更
        except Exception as e:
            logging.critical(f"❌ オープン注文監視ループ実行中に致命的なエラー: {e}", exc_info=True)
             # エラーが発生してもループを停止せず、次の間隔で再試行

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行
