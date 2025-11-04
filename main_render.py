# ====================================================================================
# Apex BOT v19.0.33 - FULL COMPLIANCE + 🚨 緊急エラー通知強化版
#
# 改良・修正点:
# 1. 【エラー処理強化】CCXTの認証エラー (AuthenticationError) やその他の致命的な例外発生時、
#    即座にTelegramで緊急通知を行い、BOTの調査/停止を促すロジックを追加しました。
# 2. 【24/7稼働】ファイルの最後にUvicornの直接実行ブロックを追加し、systemdによる永続化に対応しています。
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
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} # ★初期値を設定
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (注文IDトラッキング用)
GLOBAL_TOTAL_EQUITY: float = 0.0 # 総資産額を格納するグローバル変数

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} # 1m, 5mを含む

# 【★スコアリング定数変更 V19.0.33: 最大スコア100点に正規化 (要件4)】
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
    
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT)
        
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
        pnl_usdt = trade_result.get('pnl_usdt') if 'pnl_usdt' in trade_result else None
        pnl_rate = trade_result.get('pnl_rate') if 'pnl_rate' in trade_result else None
        filled_amount = trade_result.get('filled_amount', 0.0)

        sl_price = trade_result.get('stop_loss', 0.0)
        tp_price = trade_result.get('take_profit', 0.0)
        
        pnl_sign = "✅ 決済完了"
        pnl_line = "  - **損益**: <code>取引所履歴を確認</code>"
        if pnl_usdt is not None and pnl_rate is not None:
             pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
             pnl_line = f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price_precision(entry_price)}</code>\n"
            f"  - **決済価格 (約定価格)**: <code>{format_price_precision(exit_price)}</code>\n"
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
        f"  - **指値 (Entry)**: <code>{format_price_precision(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price_precision(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price_precision(take_profit)}</code>\n"
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
        
    message += (f"<i>Bot Ver: v19.0.33 - Limit Order & Exchange SL/TP, Score 100 Max</i>")
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
    
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()

    try:
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())

        config = {
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            }
        }
        EXCHANGE_CLIENT = exchange_class(config)
        
        await EXCHANGE_CLIENT.load_markets()
        
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化し、市場情報をロードしました。")
        
        if not API_KEY or not SECRET_KEY:
            logging.warning("⚠️ APIキーまたはシークレットキーが設定されていません。取引機能は無効です。")
            

    # 🚨 【エラー処理強化】致命的な認証エラーが発生した場合、緊急通知
    except ccxt.AuthenticationError as e:
        error_msg = f"🚨 **緊急通知: 認証エラー**\nCCXTクライアントの初期化に失敗しました。APIキーまたはシークレットキーを確認してください。BOTは停止すべきです。\nエラー詳細: `{e}`"
        logging.critical(f"❌ CCXTクライアントの初期化に失敗 (認証エラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
    # 🚨 【エラー処理強化】その他の予期せぬ致命的なエラーが発生した場合、緊急通知
    except Exception as e:
        error_msg = f"🚨 **緊急通知: 致命的な初期化エラー**\nCCXTクライアントの初期化に失敗しました。BOTは機能しません。\nエラー詳細: `{e}`"
        logging.critical(f"❌ CCXTクライアントの初期化に失敗: {e}", exc_info=True)
        await send_telegram_notification(error_msg)

async def fetch_account_status() -> Dict:
    """CCXTから口座の残高と、USDT以外の保有資産の情報を取得する。"""
    global EXCHANGE_CLIENT, GLOBAL_TOTAL_EQUITY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True} 

    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        
        GLOBAL_TOTAL_EQUITY = balance.get('total', {}).get('total', total_usdt_balance)
        if GLOBAL_TOTAL_EQUITY == 0.0:
            GLOBAL_TOTAL_EQUITY = total_usdt_balance

        open_positions = []
        for currency, amount in balance.get('total', {}).items():
            if currency not in ['USDT', 'USD'] and amount is not None and amount > 0.000001: 
                try:
                    symbol = f"{currency}/USDT"
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
                except Exception:
                    logging.warning(f"⚠️ {currency} のUSDT価値を取得できませんでした（{EXCHANGE_CLIENT.name} GET {symbol}）。")
                    
        return {
            'total_usdt_balance': total_usdt_balance,
            'total_equity': GLOBAL_TOTAL_EQUITY, 
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    # 🚨 【エラー処理強化】認証エラーが発生した場合、緊急通知
    except ccxt.AuthenticationError as e:
        error_msg = f"🚨 **緊急通知: 認証エラー (ステータス取得中)**\n口座ステータス取得時に認証エラーが発生しました。APIキーを確認してください。\nエラー詳細: `{e}`"
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
    # 🚨 【エラー処理強化】その他の致命的なエラーが発生した場合、緊急通知
    except Exception as e:
        error_msg = f"🚨 **致命的なAPIエラー**\n口座ステータス取得中に致命的なエラーが発生しました。BOTは機能しません。\nエラー詳細: `{e}`"
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)

    return {'total_usdt_balance': 0.0, 'total_equity': 0.0, 'open_positions': [], 'error': True} 


async def adjust_order_amount(symbol: str, usdt_amount: float, price: float) -> Tuple[float, float]:
    """USDT建ての注文量を取引所の最小数量、桁数に合わせて調整する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return 0.0, 0.0

    try:
        base_amount = usdt_amount / price
        
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            logging.warning(f"⚠️ {symbol}の市場情報が見つかりません。数量の丸め処理をスキップします。")
            return base_amount, usdt_amount

        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if base_amount < min_amount:
            logging.warning(f"⚠️ 注文数量 ({base_amount:.4f}) が最小取引数量 ({min_amount}) を下回りました。最小数量に調整します。")
            base_amount = min_amount

        base_amount = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount)
        final_usdt_amount = float(base_amount) * price
        
        return float(base_amount), final_usdt_amount

    except Exception as e:
        logging.error(f"❌ 注文数量の調整に失敗 ({symbol}): {e}")
        return 0.0, 0.0

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """CCXTからOHLCVデータを取得し、DataFrameに変換する (エラー処理を含む)"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCV取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが不足しています。取得数: {len(ohlcv) if ohlcv else 0}/{limit}")
            return None
            
        df = pd.DataFrame(
            ohlcv, 
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        return df

    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): 取引所が利用できません。{e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): ネットワークエラー。{e}")
    except Exception as e:
        if "Symbol not found" in str(e) or "Invalid symbol" in str(e):
             logging.warning(f"⚠️ {symbol} は取引所に存在しないためスキップします。")
             pass 
        else:
             logging.error(f"❌ OHLCV取得失敗 ({symbol} - {timeframe}): 予期せぬエラー。{e}")
             
    return None

async def fetch_fgi_data() -> Dict:
    """外部APIからFGI (Fear & Greed Index) データを取得し、マクロコンテキストを返す"""
    url = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', [])
        
        if data:
            raw_value = int(data[0]['value'])
            fgi_proxy = (raw_value - 50) / 50.0 
            
            logging.info(f"✅ FGIデータ取得成功: Raw={raw_value}, Proxy={fgi_proxy:.2f}")
            
            return {
                'fgi_raw_value': raw_value,
                'fgi_proxy': fgi_proxy,
                'forex_bonus': 0.0,
            }
            
        logging.warning("⚠️ FGIデータ取得失敗: APIデータが空です。")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得失敗 (ネットワークエラー): {e}")
        
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['SMA50'] = ta.sma(df['close'], length=50)

    df['RSI'] = ta.rsi(df['close'], length=14)

    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df[['MACD', 'MACD_H', 'MACD_S']] = macd_data

    bbands = ta.bbands(df['close'], length=20, std=2)
    df[['BBL', 'BBM', 'BBU', 'BBB', 'BBP']] = bbands
    
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    df['Volume_SMA20'] = ta.sma(df['volume'], length=20)
    
    df['Pivot'] = (df['high'].shift(1) + df['low'].shift(1) + df['close'].shift(1)) / 3
    df['R1'] = 2 * df['Pivot'] - df['low'].shift(1)
    df['S1'] = 2 * df['Pivot'] - df['high'].shift(1)

    return df


def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """分析ロジックに基づき、取引シグナルを生成する"""
    global GLOBAL_TOTAL_EQUITY, DYNAMIC_LOT_MIN_PERCENT, DYNAMIC_LOT_MAX_PERCENT, DYNAMIC_LOT_SCORE_MAX, SIGNAL_THRESHOLD
    
    if df.empty or df['SMA200'].isnull().all():
        return None
        
    current_price = df['close'].iloc[-1]
    
    if current_price > df['SMA200'].iloc[-1]:
        
        score = BASE_SCORE
        
        # --- テクニカルデータ計算 ---
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        sentiment_fgi_proxy_bonus = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD and FGI_ACTIVE_THRESHOLD > 0 else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)
        
        long_term_reversal_penalty_value = 0.0
        if current_price > df['SMA200'].iloc[-1] * 1.05:
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY 
            
        trend_alignment_bonus_value = 0.0
        if df['SMA50'].iloc[-1] > df['SMA200'].iloc[-1]:
            trend_alignment_bonus_value = TREND_ALIGNMENT_BONUS
            
        structural_pivot_bonus = 0.0
        if df['S1'].iloc[-1] < current_price and df['S1'].iloc[-1] > df['low'].iloc[-2]: 
             structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS

        macd_penalty_value = 0.0
        if df['MACD'].iloc[-1] < df['MACD_S'].iloc[-1]:
            macd_penalty_value = MACD_CROSS_PENALTY

        rsi = df['RSI'].iloc[-1]
        rsi_momentum_bonus_value = 0.0
        if rsi >= 50 and rsi < 70:
            rsi_momentum_bonus_value = RSI_MOMENTUM_BONUS_MAX * ((rsi - 50.0) / 20.0)
        
        obv_momentum_bonus_value = 0.0
        if df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1] and df['OBV'].iloc[-2] <= df['OBV_SMA'].iloc[-2]:
             obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
             
        volume_increase_bonus_value = 0.0
        if 'Volume_SMA20' in df.columns and df['Volume_SMA20'].iloc[-1] > 0 and df['volume'].iloc[-1] > df['Volume_SMA20'].iloc[-1] * 1.5:
            volume_increase_bonus_value = VOLUME_INCREASE_BONUS

        volatility_penalty_value = 0.0
        if df['BBB'].iloc[-1] < VOLATILITY_BB_PENALTY_THRESHOLD * 100:
            volatility_penalty_value = -0.05

        liquidity_bonus_value = LIQUIDITY_BONUS_MAX 

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
        
        score += (
            tech_data['trend_alignment_bonus_value'] +       
            tech_data['structural_pivot_bonus'] + 
            tech_data['rsi_momentum_bonus_value'] +          
            tech_data['obv_momentum_bonus_value'] + 
            tech_data['volume_increase_bonus_value'] +       
            tech_data['liquidity_bonus_value'] + 
            tech_data['sentiment_fgi_proxy_bonus'] + 
            tech_data['volatility_penalty_value'] - 
            tech_data['long_term_reversal_penalty_value'] -
            tech_data['macd_penalty_value']
        )
        
        
        ##############################################################
        # 2. 動的なSL/TPとRRRの設定ロジック
        ##############################################################
        
        BASE_RISK_PERCENT = 0.015
        PIVOT_SUPPORT_BONUS = tech_data.get('structural_pivot_bonus', 0.0) 
        
        sl_adjustment = (PIVOT_SUPPORT_BONUS / STRUCTURAL_PIVOT_BONUS) * 0.002 if STRUCTURAL_PIVOT_BONUS > 0 else 0.0
        dynamic_risk_percent = max(0.010, BASE_RISK_PERCENT - sl_adjustment)
        stop_loss = current_price * (1 - dynamic_risk_percent)
        
        BASE_RRR = 1.5  
        MAX_SCORE_FOR_RRR = 0.85
        MAX_RRR = 3.0
        
        current_threshold_base = get_current_threshold(macro_context)
        
        if score > current_threshold_base:
            score_ratio = min(1.0, (score - current_threshold_base) / (MAX_SCORE_FOR_RRR - current_threshold_base) if (MAX_SCORE_FOR_RRR - current_threshold_base) > 0 else 1.0)
            dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
        else:
            dynamic_rr_ratio = BASE_RRR 
            
        take_profit = current_price * (1 + dynamic_risk_percent * dynamic_rr_ratio)
        rr_ratio = dynamic_rr_ratio 
        
        ##############################################################
        # 3. 動的ロットサイズの計算 
        ##############################################################
        
        if GLOBAL_TOTAL_EQUITY > 0:
            
            normalized_score = max(0, score - SIGNAL_THRESHOLD)
            score_range = DYNAMIC_LOT_SCORE_MAX - SIGNAL_THRESHOLD
            
            if score_range > 0:
                adjustment_ratio = min(1.0, normalized_score / score_range)
            else:
                adjustment_ratio = 0.5 
            
            dynamic_percent = DYNAMIC_LOT_MIN_PERCENT + (DYNAMIC_LOT_MAX_PERCENT - DYNAMIC_LOT_MIN_PERCENT) * adjustment_ratio
            calculated_lot_size = GLOBAL_TOTAL_EQUITY * dynamic_percent
            lot_size_usdt = max(calculated_lot_size, BASE_TRADE_SIZE_USDT)
            
            logging.info(f"💰 動的ロット計算 - {symbol}: Score={score:.2f}, Ratio={dynamic_percent*100:.1f}%, Equity={GLOBAL_TOTAL_EQUITY:.2f} -> Lot={lot_size_usdt:.2f} USDT")
        else:
            lot_size_usdt = BASE_TRADE_SIZE_USDT
            logging.warning(f"⚠️ {symbol}: 総資産額が不明のため、基本ロットサイズを使用します。")
        
        ##############################################################

        # 4. 最終チェック
        current_threshold = get_current_threshold(macro_context)
        
        if score > current_threshold and rr_ratio >= 1.0:
             return {
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'buy', 
                'score': score,
                'rr_ratio': rr_ratio, 
                'entry_price': current_price,
                'stop_loss': stop_loss, 
                'take_profit': take_profit, 
                'lot_size_usdt': lot_size_usdt, 
                'tech_data': tech_data, 
            }
    return None

async def set_stop_and_take_profit(symbol: str, filled_amount: float, stop_loss: float, take_profit: float) -> Dict:
    """約定後、取引所にSL(ストップ指値)とTP(指値)注文を設定する (要件2)"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    sl_order_id = None
    tp_order_id = None
    
    try:
        # 1. TP (テイクプロフィット) 指値売り注文の設定 (Limit Sell)
        amount_to_sell, _ = await adjust_order_amount(symbol, filled_amount * take_profit, take_profit)
        
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit', 
            side='sell', 
            amount=amount_to_sell,
            price=take_profit,
            params={'timeInForce': 'GTC'}
        )
        tp_order_id = tp_order['id']
        logging.info(f"✅ TP指値売り注文成功: {symbol} @ {format_price_precision(take_profit)} (ID: {tp_order_id})")
    except Exception as e:
        logging.error(f"❌ TP注文設定失敗 ({symbol}): {e}")

    try:
        # 2. SL (ストップロス) ストップ指値売り注文の設定 (Stop Limit Sell)
        amount_to_sell, _ = await adjust_order_amount(symbol, filled_amount * stop_loss, stop_loss)
        
        sl_limit_price = stop_loss * 0.999 
        
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='stop_limit', 
            side='sell', 
            amount=amount_to_sell,
            price=sl_limit_price, 
            params={
                'stopPrice': stop_loss,
                'timeInForce': 'GTC'
            }
        )
        sl_order_id = sl_order['id']
        logging.info(f"✅ SLストップ指値売り注文成功: {symbol} トリガー@ {format_price_precision(stop_loss)} / 指値@ {format_price_precision(sl_limit_price)} (ID: {sl_order_id})")
    
    # 🚨 【エラー処理強化】致命的なエラーが発生した場合、緊急通知
    except ccxt.AuthenticationError as e:
        error_msg = f"🚨 **緊急通知: 認証エラー (SL/TP設定中)**\n{symbol}のSL/TP注文設定中に認証エラーが発生しました。BOTは停止すべきです。\nエラー詳細: `{e}`"
        logging.critical(f"❌ SL/TP設定失敗 (認証エラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
        return {'status': 'error', 'error_message': '致命的な認証エラー。'}
    except ccxt.ExchangeError as e:
        # 致命的なExchangeError（例えば、注文上限超えなど非回復性のエラー）
        error_msg = f"🚨 **緊急通知: SL/TP設定APIエラー**\n{symbol}のSL/TP注文設定中に致命的なAPIエラーが発生しました。ポジションが無防備な可能性があります。\nエラー詳細: `{e}`"
        logging.critical(f"❌ SL/TP設定失敗 (Exchange Error): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
        return {'status': 'error', 'error_message': f'致命的なSL/TP設定APIエラー: {e}'}
    except Exception as e:
        error_msg = f"🚨 **緊急通知: 予期せぬSL/TPエラー**\n{symbol}のSL/TP注文設定中に予期せぬ致命的なエラーが発生しました。\nエラー詳細: `{e}`"
        logging.critical(f"❌ SL/TP設定失敗 (予期せぬエラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
        return {'status': 'error', 'error_message': f'予期せぬ致命的エラー: {e}'}
        
    return {
        'status': 'ok',
        'sl_order_id': sl_order_id,
        'tp_order_id': tp_order_id,
    }


async def execute_trade(signal: Dict, account_status: Dict) -> Dict:
    """CCXTを利用して現物取引を実行する (指値買いに変更: 要件1)"""
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    action = signal['action']
    lot_size_usdt = signal['lot_size_usdt']
    
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

    try:
        # 1. 注文数量の調整
        limit_price = signal['entry_price']
        base_amount, final_usdt_amount = await adjust_order_amount(symbol, lot_size_usdt, limit_price)

        if base_amount <= 0:
            return {'status': 'error', 'error_message': '注文数量の調整に失敗しました。最小取引額または残高を確認してください。'}
            
        base_amount = float(EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount))
        limit_price = float(EXCHANGE_CLIENT.price_to_precision(symbol, limit_price))
            
        logging.info(f"🚀 {symbol}: {action} {base_amount:.4f} @ {limit_price:.4f} (USDT: {final_usdt_amount:.2f})")

    except Exception as e:
        return {'status': 'error', 'error_message': f'注文前処理エラー: {e}'}

    # 2. 注文実行 (指値買いに変更: limit, FOK)
    try:
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side=action,
            amount=base_amount,
            price=limit_price,
            params={'timeInForce': 'FOK'}
        )
        
        if order and order['status'] == 'closed':
            filled_amount = order['filled']
            filled_usdt = order['cost']
            
            if filled_amount > 0 and filled_usdt > 0:
                return {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    'filled_usdt': filled_usdt,
                    'id': order['id'],
                    'price': order['average'],
                    'message': 'Limit Order successfully filled (FOK).'
                }
            else:
                 return {'status': 'error', 'error_message': f"指値注文は通りましたが、約定数量がゼロです (ID: {order['id']})"}

        elif order and order['status'] in ('open', 'partial', 'canceled'):
            return {'status': 'error', 'error_message': f"指値注文は即時約定しなかったため、取引をスキップしました (ステータス: {order['status']}, ID: {order['id']})"}
        
        else:
            return {'status': 'error', 'error_message': f"注文API応答が不正です。ログを確認してください。"}


    # 🚨 【エラー処理強化】致命的なエラーが発生した場合、緊急通知
    except ccxt.AuthenticationError as e:
        error_msg = f"🚨 **緊急通知: 認証エラー (取引実行中)**\n{symbol}の取引実行中に認証エラーが発生しました。BOTは停止すべきです。\nエラー詳細: `{e}`"
        logging.critical(f"❌ 取引実行失敗 (認証エラー): {e}", exc_info=True)
        await send_telegram_notification(error_msg)
        return {'status': 'error', 'error_message': '致命的な認証エラー。'}
    except ccxt.ExchangeError as e:
        # FOK注文失敗はリカバリ可能なので通知はログのみ
        if "Fill-or-Kill" in str(e) or "was not filled" in str(e):
             return {'status': 'error', 'error_message': '指値注文は即時約定しなかったため、取引をスキップしました。'}
        # その他、非回復可能な致命的APIエラーの場合は緊急通知
        else:
             error_msg = f"🚨 **緊急通知: 取引所APIエラー**\n{symbol}の注文実行中に致命的なAPIエラーが発生しました。取引がブロックされている可能性があります。\nエラー詳細: `{e}`"
             logging.critical(f"❌ 取引実行失敗 (Exchange Error): {e}", exc_info=True)
             await send_telegram_notification(error_msg)
             return {'status': 'error', 'error_message': f'致命的な取引所APIエラー: {e}'}
    except Exception as e:
        error_msg = f"🚨 **緊急通知: 予期せぬ致命的エラー**\n{symbol}の取引実行中に予期せぬ致命的なエラーが発生しました。\nエラー詳細: `{e}`"
        logging.critical(f"❌ 予期せぬ取引エラー: {e}", exc_info=True)
        await send_telegram_notification(error_msg)
        return {'status': 'error', 'error_message': f'予期せぬ致命的取引エラー: {e}'}


async def cancel_and_remove_position(position: Dict, exit_type: str = "手動解除") -> Dict:
    """SL/TP注文をキャンセルし、ポジションリストから削除する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未準備です。'}

    symbol = position['symbol']
    sl_id = position.get('sl_order_id')
    tp_id = position.get('tp_order_id')
    
    cancel_success = True
    
    # SL/TPのキャンセルを試みる
    for order_id in [sl_id, tp_id]:
        if order_id:
            try:
                await EXCHANGE_CLIENT.cancel_order(order_id, symbol)
                logging.info(f"✅ {symbol} の注文 (ID: {order_id}) をキャンセルしました。")
            except ccxt.OrderNotFound:
                # 既に約定済みまたは取引所側でキャンセル済み
                logging.warning(f"⚠️ {symbol} の注文 (ID: {order_id}) は既に見つかりません。")
                pass
            except Exception as e:
                logging.error(f"❌ {symbol} の注文 (ID: {order_id}) のキャンセルに失敗: {e}")
                cancel_success = False

    # ポジションをOPEN_POSITIONSから削除
    global OPEN_POSITIONS
    try:
        # UUIDで完全に一致するものを削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['uuid'] != position['uuid']]
        logging.info(f"✅ {symbol} (UUID: {position['uuid']}) を管理リストから削除しました。")
        
        # 決済通知用のDictを準備 (決済価格は不明なため、0.0)
        exit_result = position.copy()
        exit_result.update({
            'exit_type': exit_type,
            'status': 'closed',
            'exit_price': 0.0,
            # pnl_usdt, pnl_rate は不明のため含めない
        })
        
        # 🚨 決済通知を送信
        await send_telegram_notification(
            format_telegram_message(
                signal={'symbol': symbol, 'timeframe': 'N/A', 'score': 0.0}, 
                context="ポジション決済", 
                current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
                trade_result=exit_result,
                exit_type=exit_type
            )
        )
        
        return {'status': 'ok', 'cancel_success': cancel_success}

    except Exception as e:
        logging.critical(f"❌ ポジションリストからの削除中に致命的なエラーが発生: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'リスト操作エラー: {e}'}


async def check_for_fills_and_cleanup(positions: List[Dict]) -> None:
    """管理中のポジションのSL/TP注文の状態を確認し、約定していたらリストから削除する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return

    positions_to_remove = []
    
    for position in positions:
        symbol = position['symbol']
        sl_id = position.get('sl_order_id')
        tp_id = position.get('tp_order_id')
        
        # SL/TP両方が存在する場合にチェック
        if sl_id and tp_id:
            try:
                # 1. TP注文 (Limit Sell) の状態を確認
                tp_order = await EXCHANGE_CLIENT.fetch_order(tp_id, symbol)
                
                if tp_order['status'] == 'closed':
                    # TPが約定した場合、SLをキャンセルして削除
                    await cancel_and_remove_position(position, exit_type="テイクプロフィット (TP)")
                    positions_to_remove.append(position)
                    continue

                # 2. SL注文 (Stop Limit Sell) の状態を確認
                sl_order = await EXCHANGE_CLIENT.fetch_order(sl_id, symbol)
                
                if sl_order['status'] == 'closed':
                    # SLが約定した場合、TPをキャンセルして削除
                    await cancel_and_remove_position(position, exit_type="ストップロス (SL)")
                    positions_to_remove.append(position)
                    continue
                    
                # どちらの注文も開いている場合
                logging.debug(f"🔍 {symbol}: SL/TP注文は引き続きオープンです。 (SL:{sl_order['status']}, TP:{tp_order['status']})")
                
            except ccxt.OrderNotFound:
                # 注文IDが見つからない = 既に約定済みかキャンセル済み
                # ポジションのステータス確認APIがないため、一旦リストから削除する (手動で確認が必要な場合もある)
                logging.warning(f"⚠️ {symbol}: SL/TP注文IDのいずれかが見つかりません。手動でポジションをチェックしてください。")
                
                # 安全のため、手動解除として処理し、通知を出す
                await cancel_and_remove_position(position, exit_type="注文不明/手動チェック要")
                positions_to_remove.append(position)
                
            except Exception as e:
                logging.error(f"❌ {symbol}: SL/TP注文のチェック中にエラーが発生: {e}")
                # エラーが発生しても、次のループで再チェックするため、ここではリストから削除しない
    
    # グローバルリストの更新は cancel_and_remove_position で行っているため、ここでは不要

# ====================================================================================
# MAIN SCHEDULERS & LOOPS
# ====================================================================================

async def main_bot_loop():
    """BOTのメイン処理: 銘柄更新 -> データ取得 -> 分析 -> 取引実行"""
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED

    logging.info("--- メインBOTループを開始します ---")
    start_time = time.time()
    
    if not IS_CLIENT_READY:
        logging.error("❌ CCXTクライアントが未準備のため、メインループをスキップします。")
        return

    # 1. マクロ環境データの取得
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()

    # 2. 口座ステータスの取得 (総資産額、現物残高の更新)
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.error("❌ 口座ステータス取得に失敗したため、取引をスキップします。")
        return

    # 3. 監視銘柄リストの更新 (ここでは静的リストを使用)
    symbols_to_monitor = CURRENT_MONITOR_SYMBOLS 
    logging.info(f"📈 監視対象銘柄数: {len(symbols_to_monitor)}")

    signals: List[Dict] = []

    # 4. 全銘柄の分析
    for symbol in symbols_to_monitor:
        try:
            # 必要な全時間足のOHLCVを取得
            ohlcv_data: Dict[str, pd.DataFrame] = {}
            for tf in TARGET_TIMEFRAMES:
                df = await fetch_ohlcv_safe(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
                if df is not None:
                    ohlcv_data[tf] = df
            
            # 最小限のデータが揃っているかチェック
            if len(ohlcv_data) < 2:
                logging.debug(f"🔍 {symbol}: データ不足のためスキップします。")
                continue

            # 複数の時間足のデータを結合してインジケーターを計算
            # ここではシンプルに、最も短い時間足 ('1m'か'5m') で分析を集中させる
            main_tf = '5m' if '5m' in ohlcv_data else ('1m' if '1m' in ohlcv_data else next(iter(ohlcv_data.keys())))
            
            df_main = calculate_indicators(ohlcv_data[main_tf])
            
            # シグナル分析
            signal = analyze_signals(df_main, symbol, main_tf, GLOBAL_MACRO_CONTEXT)
            
            if signal:
                # クールダウンチェック
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"⏸️ {symbol}: クールダウン中のためシグナルをスキップします。")
                    continue
                
                signals.append(signal)

        except Exception as e:
            logging.error(f"❌ {symbol} の分析中に予期せぬエラー: {e}", exc_info=True)
            continue

    # 5. スコア順にソートし、取引閾値を超えたシグナルをフィルタリング
    signals.sort(key=lambda x: x['score'], reverse=True)
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    tradable_signals = [s for s in signals if s['score'] >= current_threshold]
    
    LAST_ANALYSIS_SIGNALS = tradable_signals[:TOP_SIGNAL_COUNT]
    
    logging.info(f"📊 検出シグナル数: {len(signals)} | 取引可能シグナル数: {len(tradable_signals)}")

    # 6. 取引実行
    if not TEST_MODE and tradable_signals:
        
        # USDT残高チェック
        if account_status['total_usdt_balance'] < MIN_USDT_BALANCE_FOR_TRADE:
            logging.warning(f"⚠️ USDT残高が{MIN_USDT_BALANCE_FOR_TRADE} USDT未満のため、新規取引をスキップします。")
        elif len(OPEN_POSITIONS) >= 5: # 最大同時保有銘柄数
             logging.warning(f"⚠️ 最大同時保有銘柄数 (5) に達しているため、新規取引をスキップします。")
        else:
            
            signal_to_trade = tradable_signals[0] # 最高スコアのシグナルを採用
            
            # ロットサイズを再チェック (残高超過を防ぐため)
            trade_usdt_amount = min(signal_to_trade['lot_size_usdt'], account_status['total_usdt_balance'] * 0.95) # 残高の95%を上限
            
            if trade_usdt_amount < MIN_USDT_BALANCE_FOR_TRADE:
                logging.warning(f"⚠️ 調整後の取引ロット ({trade_usdt_amount:.2f} USDT) が最小ロット未満のためスキップします。")
            else:
                signal_to_trade['lot_size_usdt'] = trade_usdt_amount
                
                # 取引実行
                trade_result = await execute_trade(signal_to_trade, account_status)
                
                if trade_result['status'] == 'ok':
                    
                    # 7. SL/TP注文の設定
                    sl_tp_result = await set_stop_and_take_profit(
                        symbol=signal_to_trade['symbol'],
                        filled_amount=trade_result['filled_amount'],
                        stop_loss=signal_to_trade['stop_loss'],
                        take_profit=signal_to_trade['take_profit']
                    )
                    
                    # 8. ポジションを管理リストに追加
                    new_position = {
                        'uuid': str(uuid.uuid4()), # 注文を一意に識別するID
                        'symbol': signal_to_trade['symbol'],
                        'entry_price': trade_result['price'],
                        'filled_amount': trade_result['filled_amount'],
                        'filled_usdt': trade_result['filled_usdt'],
                        'stop_loss': signal_to_trade['stop_loss'],
                        'take_profit': signal_to_trade['take_profit'],
                        'sl_order_id': sl_tp_result.get('sl_order_id'),
                        'tp_order_id': sl_tp_result.get('tp_order_id'),
                        'status': 'open',
                        'timestamp': time.time()
                    }
                    OPEN_POSITIONS.append(new_position)
                    LAST_SIGNAL_TIME[signal_to_trade['symbol']] = time.time()

                    # 9. Telegram通知
                    final_result = signal_to_trade.copy()
                    final_result.update(trade_result)
                    final_result.update(sl_tp_result)
                    await send_telegram_notification(
                        format_telegram_message(signal_to_trade, "取引シグナル", current_threshold, final_result)
                    )
                else:
                    # 注文失敗通知
                    await send_telegram_notification(
                        format_telegram_message(signal_to_trade, "取引シグナル", current_threshold, trade_result)
                    )

    # 10. WebShareログの更新
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        log_data = {
            'analysis_time': datetime.now(JST).isoformat(),
            'signals': LAST_ANALYSIS_SIGNALS,
            'current_positions': OPEN_POSITIONS,
            'account_status': account_status,
            'macro_context': GLOBAL_MACRO_CONTEXT,
        }
        await send_webshare_update(log_data)
        
    # 11. 初回起動通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        await send_telegram_notification(
            format_startup_message(
                account_status=account_status,
                macro_context=GLOBAL_MACRO_CONTEXT,
                monitoring_count=len(symbols_to_monitor),
                current_threshold=current_threshold,
                bot_version="v19.0.33 (24/7 Enhanced)"
            )
        )
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    LAST_SUCCESS_TIME = time.time()
    elapsed = time.time() - start_time
    logging.info(f"--- メインBOTループ完了 ({elapsed:.2f}秒) ---")


async def main_loop_scheduler():
    """メインBOTループを定期実行するスケジューラ (60秒ごと)"""
    # 🚨 スケジューラ起動前の待機
    await asyncio.sleep(10) # クライアント初期化を待つ
    
    # 🚨 致命的なエラーは緊急通知を送信
    while not IS_CLIENT_READY:
        logging.warning("クライアント初期化を待機中です...")
        await asyncio.sleep(1) 

    while True:
        try:
            await main_bot_loop()
        # 🚨 【エラー処理強化】メインループ全体の例外補足時にも緊急通知
        except Exception as e:
            error_msg = f"🚨 **致命的なBOTエラー (メイン)**\nメインループで予期せぬエラーが発生しました。BOTは稼働を停止する可能性があります。\nエラー詳細: `{e}`"
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(error_msg)

        elapsed_time = time.time() - LAST_SUCCESS_TIME
        wait_time = max(1, LOOP_INTERVAL - elapsed_time)
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def open_order_management_loop_async():
    """オープン注文 (SL/TP) の監視ループ"""
    # 🚨 致命的なエラーは緊急通知を送信
    try:
        if OPEN_POSITIONS:
            await check_for_fills_and_cleanup(OPEN_POSITIONS)
        else:
            logging.debug("🔍 管理中のポジションはありません。")

    except Exception as e:
        error_msg = f"🚨 **致命的なBOTエラー (監視)**\nオープン注文監視ループで予期せぬエラーが発生しました。ポジションの防御が機能しない可能性があります。\nエラー詳細: `{e}`"
        logging.critical(f"❌ オープン注文監視ループ実行中に致命的なエラー: {e}", exc_info=True)
        await send_telegram_notification(error_msg)


async def open_order_management_scheduler():
    """オープン注文監視ループを定期実行するスケジューラ (10秒ごと)"""
    await asyncio.sleep(10) # メインループの初回実行を待つ
    while True:
        await open_order_management_loop_async()
        await asyncio.sleep(MONITOR_INTERVAL)

# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI(title="Apex BOT Trading API", version="v20.0.0")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動)"""
    # 必須のタスクを起動
    asyncio.create_task(initialize_exchange_client())
    asyncio.create_task(main_loop_scheduler())
    asyncio.create_task(open_order_management_scheduler()) 
    logging.info("(startup_event) - BOTサービスを開始しました。")

# ====================================================================================
# 🚀 永続的な実行環境 (24/7/365の実現)
# ====================================================================================
if __name__ == "__main__":
    # 24/7稼働を実現するためのUvicornの直接実行
    try:
        logging.info("--- Uvicornプロセスを起動します (ホスト: 0.0.0.0, ポート: 8000) ---")
        import uvicorn
        uvicorn.run(
            "__main__:app", 
            host="0.0.0.0", 
            port=8000, 
            log_level="info", 
            reload=False 
        )
    except Exception as e:
        logging.critical(f"❌ Uvicorn実行中に致命的なエラーが発生: {e}")
        # systemdのRestart=alwaysが再起動を試みるが、ここでは一応プロセス終了を試みる
        sys.exit(1)
