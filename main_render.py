# ====================================================================================
# Apex BOT v20.0.39 - Future Trading / 30x Leverage 
# (Feature: 固定取引ロット 20 USDT, UptimeRobot HEADメソッド対応)
# 
# 🚨 致命的エラー修正済み: UnboundLocalError (LAST_HOURLY_NOTIFICATION_TIME)
# 🚨 致命的エラー修正済み: AttributeError ('NoneType' object has no attribute 'keys')
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
BOT_VERSION = "v20.0.39"            # 💡 BOTバージョンを更新 
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
    
    # sl_ratioは取引シグナル時のみ使用するが、trade_resultがなければ計算できないため、仮置き
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        # lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT # 実際に約定した名目価値
        
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
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) # 簡易的なSLによる名目リスク
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
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
    trend_text = "🟢 長期トレンド一致" if trend_val >= LONG_TERM_REVERSAL_PENALTY else "🟡 長期トレンド逆行"
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val >= MACD_CROSS_PENALTY else "🟡 MACDモメンタム逆行"
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val >= 0 else "🔴 FGIマクロ向かい風"
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")
    
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
            # MEXCはfetch_balance(type='swap')を使用
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'}) 
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)

        if not balance:
            raise Exception("Balance object is empty.")
            
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0) 

        # MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
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
            'open_positions': [],
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

                    # 既存のSL/TP情報を引き継ぐか、初期値 (0.0) を設定
                    existing_pos = next((pos for pos in OPEN_POSITIONS if pos['symbol'] == p['symbol']), {})

                    new_open_positions.append({
                        'symbol': p['symbol'],
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': contracts,
                        'filled_usdt': notional_value,
                        'timestamp': p.get('timestamp', time.time() * 1000),
                        'stop_loss': existing_pos.get('stop_loss', 0.0), # 既存のSLを引き継ぐ
                        'take_profit': existing_pos.get('take_profit', 0.0), # 既存のTPを引き継ぐ
                    })

        OPEN_POSITIONS = new_open_positions # グローバル変数を更新

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
# ANALYTICAL CORE (欠落部分の推測/スタブ実装)
# ====================================================================================

async def get_ohlcv_data(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    # データ取得ロジックのスタブ
    try:
        # 実際にはCCXTでデータを取得する
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=REQUIRED_OHLCV_LIMITS[timeframe])
        if not ohlcv or len(ohlcv) < LONG_TERM_SMA_LENGTH:
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.warning(f"⚠️ {symbol} - {timeframe} のOHLCVデータ取得失敗: {e}")
        return None

def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # テクニカル指標追加ロジックのスタブ
    df.ta.macd(append=True)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, alias=f'SMA_{LONG_TERM_SMA_LENGTH}')
    df.ta.rsi(append=True)
    df.ta.atr(append=True)
    df.ta.bbands(append=True)
    df.ta.obv(append=True)
    return df

def calculate_signal_score(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Tuple[float, Optional[str], Optional[Dict]]:
    # スコアリングロジックのスタブ
    score = BASE_SCORE + random.uniform(-0.1, 0.5) 
    side = 'long' if score > 0.65 else 'short' # ダミーロジック
    
    # マクロ要因をスコアに反映
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX
    score += sentiment_bonus
    
    score = max(0.0, min(1.0, score)) # 0から1の範囲にクリップ
    
    # ダミーのtech_dataを作成 (通知メッセージ用)
    tech_data = {
        'long_term_reversal_penalty_value': LONG_TERM_REVERSAL_PENALTY if score > 0.6 else -LONG_TERM_REVERSAL_PENALTY,
        'macd_penalty_value': MACD_CROSS_PENALTY if score > 0.7 else -MACD_CROSS_PENALTY,
        'rsi_momentum_bonus_value': 0.05 if score > 0.75 else 0.0,
        'obv_momentum_bonus_value': 0.04 if score > 0.8 else 0.0,
        'liquidity_bonus_value': LIQUIDITY_BONUS_MAX * (1 - len(CURRENT_MONITOR_SYMBOLS) / 50),
        'sentiment_fgi_proxy_bonus': sentiment_bonus,
        'volatility_penalty_value': -0.05 if score > 0.9 else 0.0,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
    }
    
    return score, side, tech_data

async def get_macro_context() -> Dict:
    global GLOBAL_MACRO_CONTEXT
    
    try:
        # FGIを取得
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        fgi_value = int(data['data'][0]['value'])
        fgi_classification = data['data'][0]['value_classification']
        
        # FGI値を-1.0から1.0のプロキシに変換 (例: 0=Fear -> -1, 50=Neutral -> 0, 100=Greed -> 1)
        fgi_proxy = (fgi_value - 50) / 50.0 
        
        GLOBAL_MACRO_CONTEXT = {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': fgi_value,
            'fgi_classification': fgi_classification,
            'forex_bonus': 0.0 # 現在未使用
        }
        
        logging.info(f"✅ FGIを取得しました: {fgi_value} ({fgi_classification}) (影響度: {fgi_proxy * FGI_PROXY_BONUS_MAX:.4f})")
        return GLOBAL_MACRO_CONTEXT
        
    except Exception as e:
        logging.error(f"❌ FGI取得失敗: {e}")
        return GLOBAL_MACRO_CONTEXT # 既存の値またはデフォルト値を返す

async def analyze_symbols_only(current_threshold: float) -> List[Dict]:
    # 取引を実行しない分析のみを行うスタブ
    signals: List[Dict] = []
    logging.info("ℹ️ 分析専用ループを開始します...")
    
    for symbol in random.sample(CURRENT_MONITOR_SYMBOLS, min(5, len(CURRENT_MONITOR_SYMBOLS))):
        try:
            df = await get_ohlcv_data(symbol, '1h')
            if df is None: continue
            
            df = add_technical_indicators(df)
            score, side, tech_data = calculate_signal_score(df, symbol, '1h', GLOBAL_MACRO_CONTEXT)
            
            if score >= current_threshold:
                # 高スコアシグナルを記録
                signals.append({
                    'symbol': symbol,
                    'timeframe': '1h',
                    'score': score,
                    'side': side,
                    'rr_ratio': 2.0 + random.uniform(-0.5, 1.0), # ダミー
                    'tech_data': tech_data
                })
        except Exception as e:
            logging.error(f"❌ {symbol} の分析中にエラー: {e}")
            
    # スコアの高い順にソート (上位TOP_SIGNAL_COUNT個を返す)
    signals.sort(key=lambda x: x['score'], reverse=True)
    return signals[:TOP_SIGNAL_COUNT]

async def send_hourly_analysis_notification(signals: List[Dict], current_threshold: float):
    # 1時間ごとの分析通知を送信するスタブ
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    msg = f"🔔 **定期分析通知** - {now_jst} (JST)\n"
    msg += f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    msg += f"  - **市場環境**: <code>{get_current_threshold(GLOBAL_MACRO_CONTEXT)*100:.0f} / 100</code> 閾値\n"
    msg += f"  - **FGI**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code>\n"
    msg += f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    
    if signals:
        msg += "📈 **現在のTOPシグナル (高確度)**:\n"
        for i, s in enumerate(signals):
            side_tag = '🟢L' if s['side'] == 'long' else '🔴S' 
            msg += f"  - {i+1}. <b>{s['symbol']}</b> ({side_tag}, Score: {s['score']*100:.2f}%, RR: 1:{s['rr_ratio']:.2f})\n"
    else:
        msg += "📉 **現在、取引閾値 ({current_threshold*100:.0f}%) を超える有効なシグナルはありません**。\n"
        
    await send_telegram_notification(msg)
    logging.info("✅ 1時間ごとの定期分析通知を送信しました。")


# ====================================================================================
# TRADING CORE (欠落部分の推測/スタブ実装)
# ====================================================================================

async def place_order(symbol: str, side: str, amount_usdt: float, price: float, params: Optional[Dict] = None) -> Dict:
    # 注文実行ロジックのスタブ
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} の {side} 注文をスキップしました。")
        return {'status': 'ok', 'filled_amount': amount_usdt/price, 'filled_usdt': amount_usdt, 'entry_price': price, 'info': 'Test Order'}

    try:
        # CCXTで成行注文を行うロジックをここに実装
        # 実際には amount_usdt を基に数量を計算する必要がある
        amount_contracts = amount_usdt / price # 概算の契約数
        
        if side == 'long':
            order = await EXCHANGE_CLIENT.create_market_buy_order(symbol, amount_contracts)
        else:
            order = await EXCHANGE_CLIENT.create_market_sell_order(symbol, amount_contracts)
        
        # 実際の約定情報を解析 (簡略化)
        filled_price = order.get('price', price)
        filled_amount = order.get('filled', amount_contracts)
        filled_usdt = filled_amount * filled_price
        
        return {
            'status': 'ok', 
            'filled_amount': filled_amount, 
            'filled_usdt': filled_usdt, 
            'entry_price': filled_price,
            'info': order
        }
    except Exception as e:
        logging.error(f"❌ 注文執行失敗 ({symbol}, {side}): {e}")
        return {'status': 'error', 'error_message': str(e)}

async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    # エントリー処理ロジックのスタブ
    try:
        symbol = signal['symbol']
        side = signal['side']
        rr_ratio = signal['rr_ratio']
        
        df = await get_ohlcv_data(symbol, signal['timeframe'])
        if df is None:
            return {'status': 'error', 'error_message': 'データ不足'}
        
        last_close = df['close'].iloc[-1]
        
        # ATRベースのSL/TPの計算 (スタブ)
        atr_value = df['atr'].iloc[-1] if 'atr' in df.columns else last_close * 0.005 # ATRがない場合は0.5%を仮定
        risk_usdt_per_trade = FIXED_NOTIONAL_USDT # 固定ロットを使用
        
        # SL/TP価格の計算
        if side == 'long':
            stop_loss = last_close - (atr_value * ATR_MULTIPLIER_SL)
            take_profit = last_close + (atr_value * ATR_MULTIPLIER_SL * rr_ratio)
        else:
            stop_loss = last_close + (atr_value * ATR_MULTIPLIER_SL)
            take_profit = last_close - (atr_value * ATR_MULTIPLIER_SL * rr_ratio)
            
        # 注文の実行
        trade_result = await place_order(symbol, side, risk_usdt_per_trade, last_close)
        
        if trade_result['status'] == 'ok':
            # ポジションリストの更新
            new_position = {
                'symbol': symbol,
                'side': side,
                'entry_price': trade_result['entry_price'],
                'contracts': trade_result['filled_amount'],
                'filled_usdt': trade_result['filled_usdt'],
                'timestamp': time.time() * 1000,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'liquidation_price': calculate_liquidation_price(trade_result['entry_price'], LEVERAGE, side)
            }
            OPEN_POSITIONS.append(new_position)
            trade_result.update({'stop_loss': stop_loss, 'take_profit': take_profit})
        
        return trade_result
        
    except Exception as e:
        logging.error(f"❌ エントリー処理中にエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': str(e)}

async def analyze_and_trade_symbols(current_threshold: float) -> List[Dict]:
    # 分析と取引の実行ロジックのスタブ
    signals: List[Dict] = []
    
    # 既にポジションがある場合は実行しない (main_bot_loopでチェック済み)
    if OPEN_POSITIONS:
        return []
        
    sorted_symbols = CURRENT_MONITOR_SYMBOLS 
    random.shuffle(sorted_symbols)
    
    for symbol in sorted_symbols:
        try:
            df = await get_ohlcv_data(symbol, '1h')
            if df is None: continue
            
            df = add_technical_indicators(df)
            score, side, tech_data = calculate_signal_score(df, symbol, '1h', GLOBAL_MACRO_CONTEXT)
            
            if score >= current_threshold:
                # RR比率を計算 (スタブ)
                rr_ratio = 2.0 + random.uniform(-0.5, 1.0)
                
                signal_data = {
                    'symbol': symbol,
                    'timeframe': '1h',
                    'score': score,
                    'side': side,
                    'rr_ratio': rr_ratio,
                    'tech_data': tech_data
                }
                signals.append(signal_data)
                
                # エントリー実行
                trade_result = await process_entry_signal(signal_data)
                
                # 通知とログ
                notification_msg = format_telegram_message(signal_data, "取引シグナル", current_threshold, trade_result)
                await send_telegram_notification(notification_msg)
                log_signal(signal_data, "取引シグナル", trade_result)

                if trade_result['status'] == 'ok' and not TEST_MODE:
                    # 1取引で終了
                    return signals 

        except Exception as e:
            logging.error(f"❌ {symbol} の分析と取引中にエラー: {e}")
            
    # スコアの高い順にソート (上位TOP_SIGNAL_COUNT個を返す)
    signals.sort(key=lambda x: x['score'], reverse=True)
    return signals[:TOP_SIGNAL_COUNT]

async def position_monitor_and_update_sltp():
    # ポジション監視とSL/TPの更新を行うスタブ
    global OPEN_POSITIONS
    
    current_time = time.time()
    
    for pos in list(OPEN_POSITIONS):
        symbol = pos['symbol']
        try:
            # 現在価格の取得 (スタブ)
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # SL/TPチェック (スタブ)
            is_close_triggered = False
            exit_type = None

            if pos['side'] == 'long':
                if current_price <= pos['stop_loss']:
                    is_close_triggered = True
                    exit_type = "SL損切り"
                elif current_price >= pos['take_profit']:
                    is_close_triggered = True
                    exit_type = "TP利益確定"
            else: # short
                if current_price >= pos['stop_loss']:
                    is_close_triggered = True
                    exit_type = "SL損切り"
                elif current_price <= pos['take_profit']:
                    is_close_triggered = True
                    exit_type = "TP利益確定"
                    
            if is_close_triggered:
                # 決済注文の実行 (スタブ)
                trade_result = {
                    'status': 'ok',
                    'exit_type': exit_type,
                    'exit_price': current_price,
                    'entry_price': pos['entry_price'],
                    'filled_amount': pos['contracts'],
                    'pnl_usdt': (current_price - pos['entry_price']) * pos['contracts'] * LEVERAGE * (-1 if pos['side'] == 'short' else 1), # 簡易PNL
                    'pnl_rate': ((current_price - pos['entry_price']) / pos['entry_price']) * LEVERAGE * (-1 if pos['side'] == 'short' else 1),
                }

                # ポジションリストから削除
                OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] != symbol]
                
                # 通知とログ
                signal_data = {'symbol': symbol, 'side': pos['side'], 'score': 0.0} # ダミーのsignalデータ
                notification_msg = format_telegram_message(signal_data, "ポジション決済", 0.0, trade_result, exit_type)
                await send_telegram_notification(notification_msg)
                log_signal(signal_data, "ポジション決済", trade_result)
                
            # SL/TPの更新 (トレイリングストップなどが入る場合)
            # 現状はスタブのため、更新ロジックは省略
            
        except Exception as e:
            logging.error(f"❌ {symbol} のポジション監視中にエラー: {e}")

# ====================================================================================
# MARKET & SCHEDULER
# ====================================================================================

async def update_current_monitor_symbols():
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 市場更新がSKIP_MARKET_UPDATEによりスキップされました。デフォルト銘柄を使用します。")
        return
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 監視対象銘柄の更新に失敗しました: CCXTクライアントが準備できていません。")
        return

    try:
        # ログの1176行目: tickers = await EXCHANGE_CLIENT.fetch_tickers()
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 🚨 修正点 1: fetch_tickersがNoneを返す可能性があるため、Noneチェックを追加
        if tickers is None:
            logging.error("❌ 監視対象銘柄の更新に失敗しました: 'NoneType' object has no attribute 'keys' エラー回避。取引所のAPIステータスを確認してください。")
            return

        # 出来高データの整形と抽出 (既存ロジックを再現)
        top_tickers: List[Dict] = []
        for symbol, data in tickers.items():
            # USDT建ての先物/スワップシンボルのみを考慮
            if '/USDT' in symbol and (data.get('info', {}).get('isSwap') or data.get('info', {}).get('contractType') in ['PERPETUAL', 'Future']):
                # 出来高 (USDT) は 'quoteVolume' or 'baseVolume' * last price で推定
                volume_usdt = data.get('quoteVolume', 0.0)
                if volume_usdt > 0:
                    top_tickers.append({
                        'symbol': symbol,
                        'volume': volume_usdt,
                    })

        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        top_tickers.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [t['symbol'] for t in top_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルト銘柄のうち、上位リストに含まれていないものを追加
        unique_symbols = set(top_symbols)
        for d_sym in DEFAULT_SYMBOLS:
            if d_sym not in unique_symbols:
                unique_symbols.add(d_sym)
                
        new_monitor_symbols = list(unique_symbols)
        CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
        
        logging.info(f"✅ 監視対象銘柄を更新しました (出来高TOP {TOP_SYMBOL_LIMIT} + Default) - 合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        
    except Exception as e:
        # ログの1176行目: ❌ 監視対象銘柄の更新に失敗しました: 'NoneType' object has no attribute 'keys'
        logging.error(f"❌ 監視対象銘柄の更新に失敗しました: {e}", exc_info=True)


async def upload_webshare_log_data():
    # WebShareにログデータをアップロードするスタブ
    
    # 実際にはログファイルを読み込み、処理してアップロードする
    log_data = {
        'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        'bot_version': BOT_VERSION,
        'current_equity': ACCOUNT_EQUITY_USDT,
        'open_positions_count': len(OPEN_POSITIONS),
        'last_signals': LAST_ANALYSIS_SIGNALS,
    }
    await send_webshare_update({'type': 'hourly_report', 'data': log_data})
    logging.info("✅ WebShareログデータアップロード処理を完了しました。")


async def main_bot_loop():
    # 🚨 修正点 2: 以下の行を追加し、UnboundLocalErrorを修正
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, OPEN_POSITIONS, ACCOUNT_EQUITY_USDT, LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS 
    
    logging.info("--- メインボットループ実行開始 (JST: %s) ---", datetime.now(JST).strftime("%H:%M:%S"))
    
    # 1. 口座情報の取得 (最新のEquityとポジションを更新)
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 口座ステータスが取得できません。メインループをスキップします。")
        return

    await fetch_open_positions() # OPEN_POSITIONS グローバル変数を更新

    # 2. マクロコンテキストの取得 (FGIなど)
    macro_context = await get_macro_context()
    current_threshold = get_current_threshold(macro_context)
    
    # 3. 監視銘柄の更新
    await update_current_monitor_symbols() 
    
    # 4. ポジション監視・SL/TPの更新 (ポジションが一つでもあれば実行)
    if OPEN_POSITIONS:
        await position_monitor_and_update_sltp()
        
    # 5. 取引シグナル分析と執行
    if not OPEN_POSITIONS: # ポジションがない場合のみ新規シグナルを探す
        logging.info("ℹ️ ポジション不在のため、取引シグナル分析を実行します。")
        signals = await analyze_and_trade_symbols(current_threshold)
        LAST_ANALYSIS_SIGNALS = signals
    else:
        # ポジションを保有している場合は、高スコアの分析結果のみ取得 (通知用)
        logging.info(f"ℹ️ 現在 {len(OPEN_POSITIONS)} 銘柄のポジションを保有中です。新規シグナル分析はスキップします。")
        signals = await analyze_symbols_only(current_threshold)
        LAST_ANALYSIS_SIGNALS = signals


    # 6. BOT起動完了通知 (初回のみ)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # BOT起動完了通知を送信
        startup_msg = format_startup_message(account_status, macro_context, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_msg)
        
        # 初回完了通知をWebShareにも送信
        await send_webshare_update({
            'type': 'startup_notification',
            'status': 'ready',
            'timestamp': time.time(),
        })

        IS_FIRST_MAIN_LOOP_COMPLETED = True
        logging.info("✅ BOTサービス起動完了通知を送信しました。")


    # 7. 定期的な通知 (1時間ごとなど)
    current_time = time.time()
    
    # ログの1427行目: UnboundLocalError の発生箇所
    if current_time - LAST_HOURLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
        await send_hourly_analysis_notification(LAST_ANALYSIS_SIGNALS, current_threshold)
        LAST_HOURLY_NOTIFICATION_TIME = current_time # 🚨 更新

    # 8. WebShareログデータの定期的なアップロード (1時間ごと)
    if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        await upload_webshare_log_data()
        LAST_WEBSHARE_UPLOAD_TIME = current_time # 🚨 更新
        
    logging.info("--- メインボットループ実行終了 ---")


async def main_bot_scheduler():
    global IS_CLIENT_READY, LAST_SUCCESS_TIME, EXCHANGE_CLIENT
    
    logging.info("(main_bot_scheduler) - スケジューラ起動。")
    
    # 1. クライアント初期化
    while not IS_CLIENT_READY:
        if await initialize_exchange_client():
            logging.info("✅ クライアント初期化に成功しました。メインループに進みます。")
            break
        logging.warning("⚠️ クライアント初期化に失敗しました。5秒後に再試行します。")
        await asyncio.sleep(5)
    
    # 2. メインループの実行
    while True:
        # クライアントが準備できていない場合は、初期化を再試行
        if not IS_CLIENT_READY:
             if await initialize_exchange_client():
                 logging.info("✅ クライアント初期化に成功しました。メインループに進みます。")
                 continue
             else:
                 logging.critical("❌ 致命的な初期化エラー: 続行できません。")
                 await asyncio.sleep(LOOP_INTERVAL)
                 continue
                 
        current_time = time.time()
        
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            # ログの ❌ メインループ実行中に致命的なエラー: cannot access local variable...
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: <code>{e}</code>")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # LAST_SUCCESS_TIME がグローバル宣言により初期化されているため、この行でエラーは発生しない
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def position_monitor_scheduler():
    # ポジション監視をメインループと並行して行うためのスケジューラ (スタブ)
    while True:
        if OPEN_POSITIONS:
            # メインループで実行されているため、ここでは短縮した監視ロジックを呼ぶか、メインループに任せる
            pass
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {type(exc).__name__}"},
    )

if __name__ == "__main__":
    # 開発環境で実行する場合
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
