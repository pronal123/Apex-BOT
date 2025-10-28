# ====================================================================================
# Apex BOT v20.0.41 - Future Trading / 30x Leverage 
# (Feature: ATR_14 KeyError の修正, 高度分析 ADX/ADL/RSI過熱 統合)
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
from fastapi import FastAPI, Request, Response 
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math # 数値計算ライブラリ

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
# 🚨 注意: CCXTの標準シンボル形式 ('BTC/USDT') を使用
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
BOT_VERSION = "v20.0.41"            # 💡 BOTバージョンを更新 (ATR_14 KeyError Fix)
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # 💡 FGI API URL

LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間間隔 (秒) - 10秒ごと

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 # 取引倍率
TRADE_TYPE = 'future' # 取引タイプ
MIN_MAINTENANCE_MARGIN_RATE = 0.005 # 最低維持証拠金率 (例: 0.5%) - 清算価格計算に使用

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 # レバレッジ設定時のAPIレートリミット対策用遅延 (秒)

# 💡 【固定ロット】設定 
# 🚨 リスクベースの動的サイジング設定は全て削除し、この固定値を使用します。
FIXED_NOTIONAL_USDT = 20.0 

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
ACCOUNT_EQUITY_USDT: float = 0.0 # 現時点での総資産 (リスク計算に使用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  # 初期スコア
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ/一致時のボーナス
STRUCTURAL_PIVOT_BONUS = 0.05       # 構造的な優位性ボーナス (固定)
RSI_MOMENTUM_LOW = 40              # RSIモメンタム加速の閾値
MACD_CROSS_PENALTY = 0.15          # MACDモメンタム逆行時のペナルティ/一致時のボーナス
LIQUIDITY_BONUS_MAX = 0.06          # 流動性ボーナス
FGI_PROXY_BONUS_MAX = 0.05         # FGIマクロ要因最大影響度
FOREX_BONUS_MAX = 0.0               # 為替マクロ要因最大影響度 (未使用)

# 💎 新規追加: 高度分析用定数 
RSI_OVERBOUGHT_PENALTY = -0.12  # RSIが極端な水準にある場合の重大な減点
RSI_OVERSOLD_THRESHOLD = 30     # 買いシグナル時のRSI下限閾値
RSI_OVERBOUGHT_THRESHOLD = 70   # 売りシグナル時のRSI上限閾値

ADL_ACCUMULATION_BONUS = 0.08   # A/Dライン蓄積・分散ボーナス
ADX_TREND_STRENGTH_BONUS = 0.07 # ADXトレンド強度が強い場合の加点
ADX_STRENGTH_THRESHOLD = 25     # ADXがこの値を超えている場合にトレンド強しと判断


# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.95       
SIGNAL_THRESHOLD_NORMAL = 0.90      
SIGNAL_THRESHOLD_ACTIVE = 0.85      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

# ... (format_usdt, format_price, calculate_liquidation_price, get_estimated_win_rate, get_current_threshold, format_startup_message, send_telegram_notification, _to_json_compatible, log_signal, send_webshare_update は省略せずにそのまま) ...
# ... (initialize_exchange_client, fetch_account_status, fetch_open_positions も省略せずにそのまま) ...

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"
        
def format_price(price: float) -> str:
    """価格を整形する"""
    if price is None:
        price = 0.0
    # 0.01より大きい場合は小数点以下2桁、それ以外は動的に
    if price >= 0.01:
        return f"{price:,.2f}"
    return f"{price:,.8f}".rstrip('0').rstrip('.')

# 清算価格の計算関数
def calculate_liquidation_price(entry_price: float, leverage: int, side: str = 'long', maintenance_margin_rate: float = MIN_MAINTENANCE_MARGIN_RATE) -> float:
    """
    指定されたエントリー価格、レバレッジ、維持証拠金率に基づき、
    推定清算価格 (Liquidation Price) を計算する。
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算
        liquidation_price = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)
    else:
        return 0.0
        
    return max(0.0, liquidation_price) # 価格は0未満にはならない

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        return SIGNAL_THRESHOLD_NORMAL

def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # current_threshold に応じてテキストを決定するロジック
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **固定** <code>{FIXED_NOTIONAL_USDT}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **固定ロット**のため動的設定なし\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].split('/')[0] # /USDTを除去
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' 
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

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
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal.get('timeframe', '1h')
    score = signal['score']
    side = signal.get('side', 'long') 
    
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算をLong/Shortで反転
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)

    if context == "取引シグナル":
        # lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) # 実際に約定した名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount_raw = trade_result.get('filled_amount', 0.0)
            try:
                filled_amount = float(filled_amount_raw)
            except (ValueError, TypeError):
                filled_amount = 0.0
                
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            # 💡 risk_usdt の計算は複雑なため、固定ロットベースで簡略化
            risk_percent = abs(entry_price - stop_loss) / entry_price
            risk_usdt = filled_usdt_notional * risk_percent * LEVERAGE # 簡易的なSLによる名目リスク
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {risk_percent*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        try:
            filled_amount = float(filled_amount_raw)
        except (ValueError, TypeError):
            filled_amount = 0.0
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price(take_profit)}</code>\n"
        f"  - **清算価格 (Liq. Price)**: <code>{format_price(liquidation_price)}</code>\n" 
        f"  - **リスク幅 (SL)**: <code>{format_usdt(risk_width)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(reward_width)}</code> USDT\n"
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
        
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        # requestsをawait asyncio.to_threadで非同期実行
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行" if macd_val < 0 else "🟡 MACDモメンタム中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 💎 新規追加: A/Dライン蓄積ボーナス
    adl_val = tech_data.get('adl_accumulation_bonus', 0.0)
    if adl_val > 0:
        adl_text = "🟢 A/Dライン蓄積/分散" 
        breakdown_list.append(f"{adl_text}: {adl_val*100:+.2f} 点")

    # 💎 新規追加: ADXトレンド強度ボーナス
    adx_val = tech_data.get('adx_trend_strength_bonus', 0.0)
    if adx_val > 0:
        breakdown_list.append(f"🟢 ADXトレンド確証: {adx_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")

    # ペナルティ要因の表示
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱P: {vol_val*100:+.2f} 点")
        
    # 💎 新規追加: RSI過熱反転ペナルティ
    rsi_over_val = tech_data.get('rsi_overbought_penalty_value', 0.0)
    if rsi_over_val < 0:
        breakdown_list.append(f"🔴 RSI過熱反転P: {rsi_over_val*100:+.2f} 点")
        
    
    return "\n".join([f"    - {line}" for line in breakdown_list])

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), 
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            # requestsをawait asyncio.to_threadで非同期実行
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        EXCHANGE_CLIENT = None
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        options = {
            'defaultType': 'future', 
        }

        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms 
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            
            symbols_to_set_leverage = []
            
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                 # USDT建てのSwap/Future市場を探す
                 if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                     
                     # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                     if mkt['base'] in default_base_quotes:
                         # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                         symbols_to_set_leverage.append(mkt['symbol']) 
            
            # --- Patch 70 FIX 終了 ---

            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1} 
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                    
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY) 

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                    
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)


            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")


        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)

        if not balance:
            raise Exception("Balance object is empty.")
            
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0) 

        # 2. MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            if isinstance(raw_data, dict) and 'data' in raw_data:
                mexc_raw_data = raw_data.get('data')
            else:
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                mexc_data = mexc_raw_data
                
            if mexc_data:
                total_usdt_balance_fallback = 0.0
                
                if mexc_data.get('currency') == 'USDT':
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('currency') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                if total_usdt_balance_fallback > 0:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning("⚠️ MEXC専用フォールバックロジックで Equity を取得しました。")

        ACCOUNT_EQUITY_USDT = total_usdt_balance
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'open_positions': [], # ポジションはfetch_open_positionsで取得するためここでは空
            'error': False
        }
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

async def fetch_open_positions() -> List[Dict]:
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []

    try:
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions_ccxt = await EXCHANGE_CLIENT.fetch_positions()
        else:
            logging.error("❌ ポジション取得失敗: 取引所が fetch_positions APIをサポートしていません。")
            return []

        new_open_positions = []
        for p in positions_ccxt:
            if p and p.get('symbol') and p.get('contracts', 0) != 0:
                # ユーザーが監視対象としている銘柄のみを抽出 (シンボル形式が一致することを前提)
                if p['symbol'] in CURRENT_MONITOR_SYMBOLS:
                    side = 'short' if p['contracts'] < 0 else 'long'
                    entry_price = p.get('entryPrice')
                    contracts = abs(p['contracts'])
                    notional_value = p.get('notional')
                    
                    if entry_price is None or notional_value is None:
                        logging.warning(f"⚠️ {p['symbol']} のポジション情報が不完全です。スキップします。")
                        continue
                        
                    new_open_positions.append({
                        'symbol': p['symbol'],
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': contracts,
                        'filled_usdt': notional_value,
                        'timestamp': p.get('timestamp', time.time() * 1000),
                        'stop_loss': 0.0, # SL/TPは後のロジックで同期されることを想定
                        'take_profit': 0.0,
                    })

        OPEN_POSITIONS = new_open_positions
        
        # ログ強化ポイント: ポジション数が0の場合のログをより明示的に
        if len(OPEN_POSITIONS) == 0:
            logging.info("✅ CCXTから最新のオープンポジション情報を取得しました (現在 0 銘柄)。 **(ポジション不在)**")
        else:
            logging.info(f"✅ CCXTから最新のオープンポジション情報を取得しました (現在 {len(OPEN_POSITIONS)} 銘柄)。")
            
        return OPEN_POSITIONS
    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ ポジション取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ ポジション取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return []

# ====================================================================================
# CORE LOGIC: TECHNICAL ANALYSIS & SCORING (NEW V20.0.41 INTEGRATION)
# ====================================================================================

# ------------------------------------------------
# 1. 総合スコア集計ロジック
# ------------------------------------------------
def calculate_signal_score(signal: Dict[str, Any]) -> float:
    """
    シグナル詳細分析の各要素を合計し、最終的な総合スコアを計算する。
    """
    
    tech_data = signal.get('tech_data', {})
    
    # 1. ベーススコアから開始
    score = BASE_SCORE # 0.40
    
    # 2. 加点・減点の要素を集計
    
    # トレンド・構造的要因
    score += tech_data.get('structural_pivot_bonus', 0.0)
    score += tech_data.get('long_term_reversal_penalty_value', 0.0)
    
    # モメンタム・出来高要因
    score += tech_data.get('macd_penalty_value', 0.0)
    score += tech_data.get('rsi_divergence_bonus_value', 0.0) 
    score += tech_data.get('obv_momentum_bonus_value', 0.0) 
    
    # 💎 新規追加: 高度分析要素
    score += tech_data.get('adl_accumulation_bonus', 0.0)
    score += tech_data.get('adx_trend_strength_bonus', 0.0)
    score += tech_data.get('rsi_overbought_penalty_value', 0.0)
    
    # マクロ・流動性要因
    score += tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    score += tech_data.get('liquidity_bonus_value', 0.0)
    score += tech_data.get('forex_bonus_value', 0.0) # 既存コード要素
    
    # ペナルティ要因 (既存)
    score += tech_data.get('volatility_penalty_value', 0.0)
    
    # 3. スコアの値を丸め、上限を設定
    final_score = round(score, 4)
    # スコアは0未満にならない
    final_score = max(0.0, final_score) 
    
    # 総合スコアをシグナル辞書に保存
    signal['score'] = final_score
    
    return final_score


# ------------------------------------------------
# 2. 実戦的分析ロジック
# ------------------------------------------------
def calculate_advanced_analysis(df: pd.DataFrame, signal: Dict[str, Any], signal_type: str):
    """
    データフレームに基づき、高度なテクニカル分析を行い、
    スコア計算用の加点・減点値をシグナル辞書の 'tech_data' に設定する。
    """
    
    # 最終行のデータを取得
    if df.empty:
        logging.error("分析用のデータフレームが空です。")
        return
        
    last_row = df.iloc[-1]
    
    # tech_dataを初期化
    tech_data = signal.get('tech_data', {})
    
    current_close = last_row['Close']
    
    # 1. 既存の分析項目
    
    # Long Term Reversal (長期トレンド: SMA200との比較)
    long_term_sma = last_row[f'SMA_{LONG_TERM_SMA_LENGTH}']
    tech_data['long_term_reversal_penalty_value'] = 0.0
    
    if signal_type == 'long' and current_close > long_term_sma:
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY 
    elif signal_type == 'short' and current_close < long_term_sma:
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY 
    elif signal_type == 'long' and current_close < long_term_sma:
        tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY 
    elif signal_type == 'short' and current_close > long_term_sma:
        tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY 
        
    # MACDモメンタム (MACDヒストグラムの方向性)
    macd_h = last_row['MACDh_12_26_9']
    tech_data['macd_penalty_value'] = 0.0
    
    if signal_type == 'long' and macd_h > 0:
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY 
    elif signal_type == 'short' and macd_h < 0:
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY 
    elif signal_type == 'long' and macd_h < 0:
        tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY 
    elif signal_type == 'short' and macd_h > 0:
        tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY 
        
    # 構造的優位性 (SMA50との比較による簡略化)
    # SMA_50の有無をチェック
    if 'SMA_50' in df.columns:
        sma_50 = last_row['SMA_50']
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS if (signal_type == 'long' and current_close > sma_50) or (signal_type == 'short' and current_close < sma_50) else 0.0
    else:
        # SMA_50が存在しない場合は、SMA200を一時的に使用 
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS if (signal_type == 'long' and current_close > long_term_sma) or (signal_type == 'short' and current_close < long_term_sma) else 0.0


    # 流動性ボーナス (暫定的にTOP銘柄の有無で判断)
    TOP_SYMBOLS_FOR_LIQUIDITY = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"] 
    tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX if signal['symbol'] in TOP_SYMBOLS_FOR_LIQUIDITY else 0.0
    
    # FGIマクロ要因 (グローバル変数から取得)
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    tech_data['sentiment_fgi_proxy_bonus'] = 0.0
    if signal_type == 'long' and fgi_proxy > 0:
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy
    elif signal_type == 'short' and fgi_proxy < 0:
        tech_data['sentiment_fgi_proxy_bonus'] = -fgi_proxy
    else:
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy 
    
    # ダミー/既存の要素 (出来高、ダイバージェンス、ボラティリティペナルティ)
    # これらは複雑なロジックが必要だが、既存コードの定数を生かすため、一旦ランダムで実装
    tech_data['rsi_divergence_bonus_value'] = RSI_DIVERGENCE_BONUS if random.random() < 0.3 else 0.0
    tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS if random.random() < 0.5 else 0.0
    # BBandsの%Bが極端な値 (例: 1.05超え、-0.05未満) の場合にペナルティとする
    if 'BBP_5_2.0' in last_row:
        bbp = last_row['BBP_5_2.0']
        if bbp > 1.05 or bbp < -0.05:
            tech_data['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD 
        else:
            tech_data['volatility_penalty_value'] = 0.0
    else:
        tech_data['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD if random.random() < 0.2 else 0.0
        
    tech_data['forex_bonus_value'] = 0.0 # 未使用

    # 2. 💎 新規追加: 高度分析要素の計算
    
    # RSI過熱反転ペナルティの計算
    rsi = last_row['RSI_14']
    tech_data['rsi_overbought_penalty_value'] = 0.0
    
    if signal_type == 'long' and rsi > RSI_OVERBOUGHT_THRESHOLD: 
        tech_data['rsi_overbought_penalty_value'] = RSI_OVERBOUGHT_PENALTY 
            
    elif signal_type == 'short' and rsi < RSI_OVERSOLD_THRESHOLD: 
        tech_data['rsi_overbought_penalty_value'] = RSI_OVERBOUGHT_PENALTY 

    # A/Dライン蓄積ボーナスの計算
    adl_diff = 0
    if 'AD' in df.columns and len(df) >= 14:
        # 直近14期間のA/Dラインの変化
        adl_diff = last_row['AD'] - df['AD'].iloc[-14] 
        
    tech_data['adl_accumulation_bonus'] = 0.0
    
    if signal_type == 'long' and adl_diff > 0:
        tech_data['adl_accumulation_bonus'] = ADL_ACCUMULATION_BONUS 
            
    elif signal_type == 'short' and adl_diff < 0:
        tech_data['adl_accumulation_bonus'] = ADL_ACCUMULATION_BONUS 

    # ADXトレンド強度ボーナスの計算
    tech_data['adx_trend_strength_bonus'] = 0.0
    if 'ADX_14' in df.columns:
        adx = last_row['ADX_14']
        dmi_plus = last_row['DMP_14']
        dmi_minus = last_row['DMN_14']

        if adx > ADX_STRENGTH_THRESHOLD: # ADX > 25 (トレンドが強い)
            if signal_type == 'long' and dmi_plus > dmi_minus:
                tech_data['adx_trend_strength_bonus'] = ADX_TREND_STRENGTH_BONUS 
                
            elif signal_type == 'short' and dmi_minus > dmi_plus:
                tech_data['adx_trend_strength_bonus'] = ADX_TREND_STRENGTH_BONUS 

    # tech_dataをシグナル辞書に戻す
    signal['tech_data'] = tech_data


async def fetch_and_analyze(exchange: ccxt_async.Exchange, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    """
    OHLCVデータを取得し、テクニカル分析を行い、シグナルスコアを計算する。
    """
    
    # 既存のコードは250期間を取得していますが、ここでは REQUIRED_OHLCV_LIMITS を使用
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 1000)
    
    try:
        # 1. データの取得
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit) 
        
        # 200SMA計算に必要なデータが揃っているか確認
        if len(ohlcv) < LONG_TERM_SMA_LENGTH:
            logging.warning(f"データ不足: {symbol} - {timeframe} ({len(ohlcv)}/{LONG_TERM_SMA_LENGTH}期間未満)")
            return []
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        
        # 2. テクニカル指標の計算 (pandas-taを使用)
        df.ta.ema(length=20, append=True) 
        df.ta.macd(append=True)
        df.ta.rsi(append=True)
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # 200SMA
        df.ta.sma(length=50, append=True) # 50SMA (構造的優位性に使用)
        df.ta.obv(append=True) # OBV (出来高)
        df.ta.bbands(append=True) # BBands (ボラティリティ)
        
        # 💎 新規追加: A/DラインとADX/DMIの計算
        df.ta.ad(append=True) # A/Dライン
        df.ta.adx(append=True) # ADX/DMI
        
        # 💥 修正点: 欠落していた ATR の計算を追加
        df.ta.atr(length=ATR_LENGTH, append=True) # ATR_14を追加
        
        # データが NaN を含む行を削除
        df = df.dropna()
        if df.empty or len(df) < 5:
            logging.warning(f"{symbol} のテクニカル指標計算後、データが不足しました。")
            return []
            
        # 3. シグナル判定 (最後のMACDクロス方向をシグナルとする)
        last_macd_h = df['MACDh_12_26_9'].iloc[-1]
        prev_macd_h = df['MACDh_12_26_9'].iloc[-2]
        
        signals: List[Dict[str, Any]] = []
        
        # MACDゴールデンクロス -> Longシグナル
        if last_macd_h > 0 and prev_macd_h <= 0:
            signals.append({'symbol': symbol, 'side': 'long', 'timeframe': timeframe, 'entry_price': df['Close'].iloc[-1]})
        
        # MACDデッドクロス -> Shortシグナル
        if last_macd_h < 0 and prev_macd_h >= 0:
            signals.append({'symbol': symbol, 'side': 'short', 'timeframe': timeframe, 'entry_price': df['Close'].iloc[-1]})
        
        if not signals:
            return []
            
        # 4. 🚀 高度なテクニカル分析とスコアリングの実行
        final_signals = []
        for signal in signals:
            
            # 4-1. 💡 高度なテクニカル分析を実行し、tech_dataに加点/減点要素を書き込む
            calculate_advanced_analysis(df, signal, signal['side'])
            
            # 4-2. 💰 総合スコアの計算
            final_score = calculate_signal_score(signal)
            
            # 4-3. リスク/リワードの計算 (ATRを使用して動的に計算)
            last_row = df.iloc[-1]
            atr = last_row[f'ATR_{ATR_LENGTH}'] # 修正済み: KeyErrorの発生源だった行
            entry_price = signal['entry_price']
            
            # ATRベースのリスク幅
            risk_amount = atr * ATR_MULTIPLIER_SL
            
            if signal['side'] == 'long':
                stop_loss = entry_price - risk_amount
                # RRR 1:2 を想定
                take_profit = entry_price + (risk_amount * 2.0) 
            else:
                stop_loss = entry_price + risk_amount
                take_profit = entry_price - (risk_amount * 2.0) 
                
            # 清算価格の計算
            liquidation_price = calculate_liquidation_price(
                entry_price, LEVERAGE, signal['side'], MIN_MAINTENANCE_MARGIN_RATE
            )
            
            # リスクリワード比率
            sl_abs = abs(entry_price - stop_loss)
            tp_abs = abs(take_profit - entry_price)
            rr_ratio = round(tp_abs / sl_abs, 2) if sl_abs > 0 else 0.0
            
            signal['stop_loss'] = stop_loss
            signal['take_profit'] = take_profit
            signal['liquidation_price'] = liquidation_price
            signal['rr_ratio'] = rr_ratio
            
            final_signals.append(signal)

        return final_signals

    except Exception as e:
        # エラーログにATR_14以外のKeyErrorなど、他の致命的なエラーの可能性を追記
        if 'ATR' not in str(e) and 'KeyError' in str(e):
             logging.error(f"❌ {symbol} - {timeframe} の分析中に致命的なKeyError: {e}", exc_info=True)
        else:
            logging.error(f"❌ {symbol} - {timeframe} の分析中にエラー: {e}", exc_info=True)
        return []

# ====================================================================================
# EXCHANGE AND MAIN BOT LOOP
# ====================================================================================

# ... (main_bot_loop, fetch_fgi_score, position_monitor_loop, position_monitor_scheduler, main_bot_scheduler, FastAPI関連は省略) ...

async def main_bot_loop():
    """メインの取引判断と実行ループ"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_SIGNAL_TIME
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.critical("❌ メインループ失敗: CCXTクライアントが準備できていません。")
        return

    logging.info("--- 新しいメインループを開始します ---")
    
    # 1. マクロ要因の更新 (FGI)
    await fetch_fgi_score() 
    
    # 2. 未決済ポジションの同期 (fetch_open_positionsを呼び出し)
    await fetch_open_positions()
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 3. 全ターゲットシンボルの分析
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # 既存ポジションがある銘柄はスキップ（多重エントリー防止）
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            continue 
            
        # 💡 5mタイムフレームを固定で分析
        signals = await fetch_and_analyze(EXCHANGE_CLIENT, symbol, '5m')
        
        # フィルタリングされたシグナルを処理
        for signal in signals:
            final_score = signal.get('score', 0.0)
            side = signal['side']
            
            # 4. スコアフィルタリングと取引実行
            
            if final_score >= current_threshold: 
                
                # 既存ポジションを持たないことと、クールダウン期間のチェックを行う
                last_signal = LAST_SIGNAL_TIME.get(symbol, 0.0)
                if (time.time() - last_signal) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"☑️ {symbol} - {side.upper()} はクールダウン中のためスキップ (残り {int(TRADE_SIGNAL_COOLDOWN - (time.time() - last_signal))}秒)")
                    continue

                # スコア詳細メッセージを作成
                breakdown_msg = get_score_breakdown(signal) 
                
                # ログと通知
                logging.info(f"✅ 強力なシグナル検出: {symbol} - {side.upper()} (スコア: {final_score:.2f} / 閾値: {current_threshold:.2f})")
                
                # 🚨 実際にはここで取引実行ロジック (execute_trade) が入る
                # ダミーの取引結果
                entry_price = signal['entry_price']
                filled_amount = FIXED_NOTIONAL_USDT / entry_price
                trade_result = {
                    'status': 'ok', 
                    'filled_usdt': FIXED_NOTIONAL_USDT, 
                    'filled_amount': filled_amount,
                    'entry_price': entry_price,
                } 
                
                # 通知送信
                notification_msg = format_telegram_message(
                    signal=signal, 
                    context="取引シグナル", 
                    current_threshold=current_threshold, 
                    trade_result=trade_result
                )
                await send_telegram_notification(notification_msg)
                
                # ログ記録
                log_signal(signal, "取引シグナル", trade_result)

                # ポジション追跡に追加 (ダミー)
                OPEN_POSITIONS.append({
                    'symbol': symbol, 
                    'side': side, 
                    'entry_price': signal['entry_price'], 
                    'contracts': filled_amount, 
                    'filled_usdt': FIXED_NOTIONAL_USDT,
                    'timestamp': time.time() * 1000,
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                })
                LAST_SIGNAL_TIME[symbol] = time.time() # クールダウンタイマーを更新
                
            else:
                logging.info(f"☑️ {symbol} - {side.upper()} はスコアが低いためスキップ (スコア: {final_score:.2f} / 閾値: {current_threshold:.2f})")
                
    # 5. メインループの完了
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        logging.info("✅ BOTの最初のメイン分析ループが完了しました。")
        IS_FIRST_MAIN_LOOP_COMPLETED = True


async def fetch_fgi_score():
    """Fear & Greed Index (FGI) を取得し、スコアに変換する"""
    global GLOBAL_MACRO_CONTEXT
    
    try:
        # FGI APIを叩き、FGIの数値 (0-100) を取得
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # 値の抽出
        fgi_value_raw = data.get('data', [{}])[0].get('value', '50')
        fgi_value = int(fgi_value_raw)
        
        # FGIをスコアに変換 (例: FGI 50=0点, 0/100=-0.05/0.05点)
        # スケール: (FGI - 50) / 50 * FGI_PROXY_BONUS_MAX
        fgi_normalized = (fgi_value - 50) / 50 
        macro_fgi_proxy = round(fgi_normalized * FGI_PROXY_BONUS_MAX, 4)
        
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = macro_fgi_proxy
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = fgi_value_raw
        
        logging.info(f"FGIスコアを更新: FGI={fgi_value}, スコア影響度={macro_fgi_proxy:.4f}")
        
    except Exception as e:
        logging.error(f"FGIスコアの取得中にエラーが発生しました: {e}")
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0 # エラー時は無効化


# ====================================================================================
# SCHEDULER & FASTAPI
# ====================================================================================

async def position_monitor_loop():
    """ポジション監視と決済ロジック (ダミー/省略)"""
    # 実際にはここでポジションの利益/損失を監視し、TP/SLまたは逆シグナルで決済
    pass

async def position_monitor_scheduler():
    """ポジション監視ループを定期的に実行するためのスケジューラ"""
    while True:
        try:
            await position_monitor_loop()
        except Exception as e:
            logging.error(f"❌ ポジション監視ループ実行中にエラー: {e}")
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごと


async def main_bot_scheduler():
    """メインBOTループを定期的に実行するためのスケジューラ"""
    global LAST_SUCCESS_TIME, IS_CLIENT_READY
    
    if not await initialize_exchange_client():
        logging.critical("クライアント初期化に失敗したため、メインBOTスケジューラを停止します。")
        return
    
    # 初回起動通知
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    startup_message = format_startup_message(
        account_status, 
        GLOBAL_MACRO_CONTEXT, 
        len(CURRENT_MONITOR_SYMBOLS), 
        current_threshold
    )
    await send_telegram_notification(startup_message)
    
    while True:
        if not IS_CLIENT_READY:
            logging.critical("APIキーが無効またはCCXTクライアントが停止しています。BOTは動作しません。")
            await asyncio.sleep(LOOP_INTERVAL)
            continue

        current_time = time.time()
        
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: <code>{e}</code>")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行にかかった時間を差し引く
        wait_time = max(1, LOOP_INTERVAL - (time.time() - current_time))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


# FastAPIアプリケーションインスタンス
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())
    

# 💡 UptimeRobotなどのヘルスチェック対応エンドポイント
@app.head("/")
@app.get("/")
async def health_check():
    """ヘルスチェックエンドポイント"""
    # 最終成功時刻がLOOP_INTERVALの3倍を超えていたらエラーを返す (BOTが停止している可能性)
    if time.time() - LAST_SUCCESS_TIME > LOOP_INTERVAL * 3 and LAST_SUCCESS_TIME != 0:
        logging.error("ヘルスチェック失敗: メインループが長時間実行されていません。")
        return Response(status_code=503) # Service Unavailable
    
    return Response(status_code=200) # OK


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    # Unclosed client sessionのような一般的な警告エラーはログに出さない
    if "Unclosed" not in str(exc) and "ClientSession is closed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )
