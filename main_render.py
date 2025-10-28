# ====================================================================================
# Apex BOT v20.0.40 - Future Trading / 30x Leverage 
# (Feature: 固定取引ロット 20 USDT, UptimeRobot HEADメソッド対応)
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
from fastapi import FastAPI, Request, Response # 💡 Request, Responseをインポート
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
BOT_VERSION = "v20.0.40"            # 💡 BOTバージョンを更新 
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

    # sl_ratioの計算 (未定義エラーの解消)
    sl_ratio = 0.0
    if entry_price > 0:
        sl_ratio = risk_width / entry_price # SL幅の対エントリー価格比率

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
            # risk_usdt の計算を修正
            risk_usdt = abs(filled_usdt_notional) * sl_ratio # 名目ロット * SL比率
            
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
            # ログメッセージの末尾の省略記号を削除し、行を完了させる
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
                    notional_value = p.get('notional') # 名目価値 (USDT)
                    
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
                        'stop_loss': 0.0, # 初期値
                        'take_profit': 0.0, # 初期値
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
# ANALYTICAL CORE
# ====================================================================================

def _calculate_ta_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたデータフレームにテクニカル分析指標を追加する。
    """
    if df.empty:
        return df

    # 1. ボリューム指標 (OBV)
    df.ta.obv(append=True)
    
    # 2. トレンド指標 (SMA, MACD)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.macd(append=True)
    
    # 3. モメンタム指標 (RSI)
    df.ta.rsi(append=True)
    
    # 4. ボラティリティ指標 (ATR, BBANDS)
    df.ta.atr(length=ATR_LENGTH, append=True)
    df.ta.bbands(append=True)
    
    # 指標名が重複しないように、特定の取引所固有のプレフィックスを削除/修正 (必要に応じて)
    # df.columns = [col.replace('MACD_', 'MACD') for col in df.columns]

    return df.dropna(subset=[f'SMA_{LONG_TERM_SMA_LENGTH}', 'RSI_14', 'MACDh_12_26_9', f'ATR_{ATR_LENGTH}'])


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、DataFrameとして返す。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗: クライアントが準備できていません。({symbol}, {timeframe})")
        return None
    
    try:
        # ccxtのfetch_ohlcvはタイムアウトしやすいので、再試行ロジックを追加
        retries = 3
        for i in range(retries):
            try:
                ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
                
                if not ohlcv:
                    logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが空です。")
                    return None
                    
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
                df.set_index('datetime', inplace=True)
                
                if len(df) < limit:
                    logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なデータ数 ({limit}) に対して、取得できたのは {len(df)} のみです。")
                
                return df

            except ccxt.RateLimitExceeded as e:
                # レートリミット時のロジック。指数バックオフを推奨。
                delay = (i + 1) * 2 
                logging.warning(f"⚠️ {symbol} ({timeframe}) でレートリミット: {e}。{delay}秒待機後に再試行します...")
                await asyncio.sleep(delay)
                continue
                
            except ccxt.NetworkError as e:
                logging.warning(f"⚠️ {symbol} ({timeframe}) でネットワークエラー: {e}。再試行します...")
                await asyncio.sleep(2)
                continue
                
        # 全ての再試行が失敗
        logging.error(f"❌ {symbol} ({timeframe}): OHLCVデータの取得に失敗しました (再試行 {retries}回失敗)。")
        return None

    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): OHLCVデータ取得中に予期せぬエラー: {e}", exc_info=True)
        return None


async def analyze_symbol(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    指定された銘柄について、複数の時間軸でテクニカル分析を行い、
    取引シグナル (score > 0) のリストを返す。
    """
    signals: List[Dict] = []
    
    for timeframe in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 1000)
        df = await fetch_ohlcv(symbol, timeframe, limit)
        
        if df is None or df.empty or len(df) < 50:
            logging.warning(f"⚠️ {symbol} ({timeframe}): データ不足のためスキップします。")
            continue
            
        # テクニカル指標の計算
        df = _calculate_ta_indicators(df)
        if df.empty:
            logging.warning(f"⚠️ {symbol} ({timeframe}): TA計算後、データ不足のためスキップします。")
            continue
            
        # 最新のローソク足のデータを使用
        last = df.iloc[-1]
        
        # 最後に確定したローソク足 (df.iloc[-2]) の価格情報
        # エントリーは成行なので、最新のcloseを使用しても良いが、ここではlastを使用
        # prev = df.iloc[-2]

        current_price = last['close']
        
        # スコアリングロジック
        score_long, tech_data_long = score_signal(df, current_price, 'long', timeframe, macro_context)
        score_short, tech_data_short = score_signal(df, current_price, 'short', timeframe, macro_context)
        
        
        # Longシグナルの生成
        if score_long > BASE_SCORE:
            signal_long = generate_signal(symbol, timeframe, 'long', current_price, df, score_long, tech_data_long)
            if signal_long:
                signals.append(signal_long)
                
        # Shortシグナルの生成
        if score_short > BASE_SCORE:
            signal_short = generate_signal(symbol, timeframe, 'short', current_price, df, score_short, tech_data_short)
            if signal_short:
                signals.append(signal_short)
                
        # APIレートリミット対策で時間軸ごとの遅延を挿入
        await asyncio.sleep(0.01) 
        
    # スコアの高い順にソート (最も有望なシグナルを優先)
    signals.sort(key=lambda x: x['score'], reverse=True)
    return signals


def score_signal(df: pd.DataFrame, current_price: float, side: str, timeframe: str, macro_context: Dict) -> Tuple[float, Dict]:
    """
    単一時間軸・単一方向の取引シグナルスコアを計算する。
    """
    last = df.iloc[-1]
    
    score = BASE_SCORE
    tech_data: Dict[str, Any] = {}
    
    # 1. 長期トレンドとの一致/逆行 (SMA 200)
    sma_200 = last[f'SMA_{LONG_TERM_SMA_LENGTH}']
    trend_match = (side == 'long' and current_price > sma_200) or \
                  (side == 'short' and current_price < sma_200)
    
    trend_penalty_value = LONG_TERM_REVERSAL_PENALTY if trend_match else -LONG_TERM_REVERSAL_PENALTY
    score += trend_penalty_value
    tech_data['long_term_reversal_penalty_value'] = trend_penalty_value
    
    # 2. MACDモメンタムとの一致/逆行 (MACD Histogram)
    macd_hist = last['MACDh_12_26_9']
    macd_match = (side == 'long' and macd_hist > 0) or \
                 (side == 'short' and macd_hist < 0)
                 
    macd_penalty_value = MACD_CROSS_PENALTY if macd_match else -MACD_CROSS_PENALTY
    score += macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # 3. RSIモメンタムの加速/買われすぎ/売られすぎ
    rsi = last['RSI_14']
    rsi_bonus_value = 0.0
    
    if side == 'long':
        # ロングの場合: 40以上への加速 (モメンタム加速)
        if rsi >= RSI_MOMENTUM_LOW and rsi < 70:
            rsi_bonus_value = OBV_MOMENTUM_BONUS # 出来高確証と同じボーナス
            score += rsi_bonus_value
        # 買われすぎはペナルティ
        elif rsi >= 70:
            score -= 0.05
            
    elif side == 'short':
        # ショートの場合: 60以下への加速 (モメンタム加速)
        if rsi <= (100 - RSI_MOMENTUM_LOW) and rsi > 30:
            rsi_bonus_value = OBV_MOMENTUM_BONUS
            score += rsi_bonus_value
        # 売られすぎはペナルティ
        elif rsi <= 30:
            score -= 0.05
            
    tech_data['rsi_momentum_bonus_value'] = rsi_bonus_value

    # 4. OBVによる出来高確証 (直近3期間でOBVが増加/減少)
    obv = df['OBV'].iloc[-3:]
    obv_match_long = (obv.iloc[0] < obv.iloc[1] < obv.iloc[2])
    obv_match_short = (obv.iloc[0] > obv.iloc[1] > obv.iloc[2])
    
    obv_bonus_value = 0.0
    if side == 'long' and obv_match_long:
        obv_bonus_value = OBV_MOMENTUM_BONUS
        score += obv_bonus_value
    elif side == 'short' and obv_match_short:
        obv_bonus_value = OBV_MOMENTUM_BONUS
        score += obv_bonus_value
        
    tech_data['obv_momentum_bonus_value'] = obv_bonus_value

    # 5. ボラティリティによるペナルティ (BBANDSの幅)
    bb_width = last['BBP_14_2.0']
    volatility_penalty_value = 0.0
    
    if bb_width > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty_value = -0.05
        score += volatility_penalty_value # ボラティリティが過熱している場合はペナルティ
        
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # 6. 構造的優位性 (常にボーナス)
    score += STRUCTURAL_PIVOT_BONUS
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
    
    # 7. 流動性ボーナス (銘柄がTOPリストにいること)
    liquidity_bonus_value = 0.0
    if symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]:
        # TOP銘柄ほどボーナスを大きくする (リスト内のインデックスに基づいて)
        rank = CURRENT_MONITOR_SYMBOLS.index(symbol) if symbol in CURRENT_MONITOR_SYMBOLS else TOP_SYMBOL_LIMIT
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * (1 - (rank / TOP_SYMBOL_LIMIT))
        score += liquidity_bonus_value
        
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # 8. FGIマクロ要因の調整
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = 0.0
    
    if side == 'long' and fgi_proxy > 0:
        # ロングでマクロがポジティブならボーナス
        sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, fgi_proxy * FGI_PROXY_BONUS_MAX * 2) 
    elif side == 'short' and fgi_proxy < 0:
        # ショートでマクロがネガティブならボーナス
        sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, abs(fgi_proxy) * FGI_PROXY_BONUS_MAX * 2) 
    elif side == 'long' and fgi_proxy < 0:
        # ロングでマクロがネガティブならペナルティ
        sentiment_fgi_proxy_bonus = max(-FGI_PROXY_BONUS_MAX, fgi_proxy * FGI_PROXY_BONUS_MAX * 2) 
    elif side == 'short' and fgi_proxy > 0:
        # ショートでマクロがポジティブならペナルティ
        sentiment_fgi_proxy_bonus = max(-FGI_PROXY_BONUS_MAX, -fgi_proxy * FGI_PROXY_BONUS_MAX * 2) 
        
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

    # スコアの範囲を強制 (0.0から1.0)
    final_score = max(0.0, min(1.0, score))
    
    # 最終的なマクロ調整後の閾値を記録
    tech_data['dynamic_threshold'] = get_current_threshold(macro_context)
    
    return final_score, tech_data


def generate_signal(symbol: str, timeframe: str, side: str, entry_price: float, df: pd.DataFrame, score: float, tech_data: Dict) -> Optional[Dict]:
    """
    計算されたスコアと価格情報から、具体的な取引シグナルオブジェクトを生成する。
    SL/TPの計算はATRを使用する。
    """
    last = df.iloc[-1]
    
    # ATRに基づくリスク幅 (SLまでの価格差)
    atr = last[f'ATR_{ATR_LENGTH}']
    
    # ATR_MULTIPLIER_SL倍のATRをリスク幅とする
    risk_width = atr * ATR_MULTIPLIER_SL

    # リスク幅の最小パーセンテージチェック
    min_risk_width = entry_price * MIN_RISK_PERCENT
    risk_width = max(risk_width, min_risk_width)
    
    # シンプルに固定のRRRを使用
    RRR_ratio = 2.0 
    
    reward_width = risk_width * RRR_ratio
    
    stop_loss = 0.0
    take_profit = 0.0
    
    if side == 'long':
        stop_loss = entry_price - risk_width
        take_profit = entry_price + reward_width
    elif side == 'short':
        stop_loss = entry_price + risk_width
        take_profit = entry_price - reward_width
        
    # SL/TPがエントリー価格に対して0.0より大きいことを確認
    if stop_loss <= 0.0 or take_profit <= 0.0:
        logging.warning(f"⚠️ {symbol} ({side}, {timeframe}): SL/TP計算エラー (価格 <= 0.0)。スキップ。")
        return None
        
    # 清算価格の計算
    liquidation_price = calculate_liquidation_price(entry_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)

    # 最終シグナルオブジェクトの構築
    signal = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': liquidation_price,
        'score': score,
        'rr_ratio': RRR_ratio,
        'risk_width_usdt': risk_width, 
        'reward_width_usdt': reward_width,
        'tech_data': tech_data, 
        'timestamp': time.time()
    }
    
    return signal


async def get_macro_context() -> Dict:
    """FGI (Fear & Greed Index) および為替マクロ情報を取得し、マクロコンテキストを更新する。"""
    
    # 1. FGIの取得と正規化
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        fgi_value = int(data['data'][0]['value']) # 0-100
        fgi_value_text = data['data'][0]['value_classification']
        
        # 0.0 -> Fear (0), 1.0 -> Greed (100) に正規化
        fgi_proxy = (fgi_value / 100.0) * 2 - 1.0 # -1.0 (Extreme Fear) から +1.0 (Extreme Greed)

        # 閾値 (-0.02 ~ 0.02) 内に収まるように調整 (過度な影響を避けるため)
        fgi_proxy_normalized = max(-FGI_PROXY_BONUS_MAX*2, min(FGI_PROXY_BONUS_MAX*2, fgi_proxy))
        
        GLOBAL_MACRO_CONTEXT.update({
            'fgi_proxy': fgi_proxy_normalized, # 最終的にスコア計算に使用される値
            'fgi_raw_value': f"{fgi_value} ({fgi_value_text})", # 通知用の表示値
        })
        logging.info(f"✅ FGIを取得しました: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']} (影響度: {GLOBAL_MACRO_CONTEXT['fgi_proxy']:.4f})")
        
    except Exception as e:
        logging.error(f"❌ FGI取得失敗: {e}")
        # 失敗した場合、コンテキストを中立に戻す
        GLOBAL_MACRO_CONTEXT.update({
            'fgi_proxy': 0.0,
            'fgi_raw_value': 'N/A (取得失敗)',
        })
        
    # 2. 為替マクロ要因 (現在のバージョンでは未使用)
    # 現在のところ、FOREX_BONUS_MAX = 0.0 のため、常に0.0
    GLOBAL_MACRO_CONTEXT['forex_bonus'] = 0.0
    
    return GLOBAL_MACRO_CONTEXT


async def update_current_monitor_symbols() -> None:
    """取引所の出来高TOP銘柄を取得し、監視対象リストを更新する。"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。監視銘柄の更新をスキップします。")
        return 
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 監視銘柄更新失敗: CCXTクライアントが準備できていません。")
        return
        
    try:
        # fetch_tickersで全市場の情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建ての先物/スワップ市場をフィルタリングし、24hボリュームでソート
        usdt_future_tickers = []
        for symbol, ticker in tickers.items():
            
            # シンボル形式が'BASE/USDT' (現物) か 'BASE/USDT:USDT' (先物) かを確認
            # 出来高 (quoteVolume) が存在することを確認
            is_usdt_future = False
            is_active = ticker.get('active', True)
            
            if 'USDT' in symbol and (EXCHANGE_CLIENT.markets.get(symbol, {}).get('type') in ['swap', 'future'] or symbol.endswith('/USDT')):
                if ticker.get('quoteVolume', 0.0) is not None and ticker.get('quoteVolume', 0.0) > 0:
                    is_usdt_future = True
            
            if is_usdt_future and is_active:
                usdt_future_tickers.append(ticker)
                
        # 24hのQuote Volume (USDT)で降順ソート
        usdt_future_tickers.sort(key=lambda x: x.get('quoteVolume', 0.0), reverse=True)
        
        # Top N銘柄を選出
        top_symbols = [t['symbol'] for t in usdt_future_tickers[:TOP_SYMBOL_LIMIT]]
        
        # DEFAULT_SYMBOLSに含まれているが、TOPから漏れたシンボルを追加する
        for d_symbol in DEFAULT_SYMBOLS:
            if d_symbol not in top_symbols:
                top_symbols.append(d_symbol)
                
        CURRENT_MONITOR_SYMBOLS = top_symbols
        logging.info(f"✅ 監視対象銘柄リストを更新しました。合計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")

    except Exception as e:
        logging.error(f"❌ 監視対象銘柄の更新に失敗しました: {e}", exc_info=True)
        # 失敗した場合、既存のリスト(DEFAULT_SYMBOLS)を維持

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

async def execute_trade(signal: Dict) -> Dict:
    """
    CCXTを使用して実際に取引を執行する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    if TEST_MODE:
        return {'status': 'test_mode', 'filled_usdt': FIXED_NOTIONAL_USDT} # テストモードではダミーの結果を返す

    try:
        symbol = signal['symbol']
        side = signal['side']
        entry_price = signal['entry_price']
        stop_loss = signal['stop_loss']
        take_profit = signal['take_profit']
        
        # 💡 【固定ロット】取引量計算
        # 常に固定の名目価値(FIXED_NOTIONAL_USDT)を取引します。
        # CCXTのcreate_orderに渡す'amount' (取引数量) を計算する必要があります。
        
        # amount (コイン数量) = Notional Value (USDT) / Current Price
        amount = FIXED_NOTIONAL_USDT / entry_price
        
        # 取引所の最小取引量の丸め処理が必要だが、ccxtが自動で行うことを期待
        # 例: BTC/USDTで $20ロット -> 20 / 60000 = 0.000333... BTC
        
        # 注文タイプ (買い: 'long', 売り: 'short')
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 成行注文を執行
        # MEXCの場合、先物取引は 'swap' タイプのシンボル (例: BTC/USDT) を使用
        
        params = {
            'stopLoss': stop_loss,
            'takeProfit': take_profit,
            # 'defaultType': 'swap' # MEXCの場合は初期化時に設定済み
        }
        
        # ⚠️ CCXTのUnified Margin/Future APIでは、ポジションのSL/TPは個別に設定する必要があります。
        # ほとんどの取引所では、create_orderでSL/TPを指定しても、約定後のポジションに自動で設定されないか、
        # 成行注文のパラメータとして受け付けられません。
        # したがって、ここでは成行注文のみを行い、SL/TPはポジション監視ループで設定/管理します。
        
        # 成行注文 (Market Order)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', # 注文タイプ: 成行
            order_side,
            amount, # 数量 (BASE単位: 例: BTC)
            params={}
        )

        # 約定結果の確認と整形
        if order and order['status'] in ['closed', 'open', 'filled']:
            
            # 約定価格、約定数量、名目価値 (filled amount) を取得
            filled_amount = order.get('filled', amount)
            filled_usdt_notional = order.get('cost', FIXED_NOTIONAL_USDT) # コスト (USDT)
            entry_price_final = order.get('price', entry_price)

            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'entry_price': entry_price_final,
            }
        
        else:
            return {'status': 'error', 'error_message': f"注文ステータス異常: {order.get('status', '不明')}"}
            
    except ccxt.InsufficientFunds as e:
        error_msg = f"証拠金不足エラー: {e}"
        logging.error(f"❌ 取引執行失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.ExchangeError as e:
        error_msg = f"取引所エラー (CCXT): {e}"
        logging.error(f"❌ 取引執行失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ 取引執行失敗: {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}


async def close_position(position: Dict, exit_type: str) -> Dict:
    """
    ポジションを決済する (成行決済)。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    if TEST_MODE:
        # テストモードではダミーの結果を返す
        pnl_rate_dummy = 0.05 if exit_type == 'TP' else -0.05
        pnl_usdt_dummy = position['filled_usdt'] * pnl_rate_dummy
        exit_price_dummy = position['entry_price'] * (1 + pnl_rate_dummy)
        
        return {
            'status': 'ok', 
            'exit_type': exit_type,
            'entry_price': position['entry_price'],
            'exit_price': exit_price_dummy,
            'filled_amount': position['contracts'],
            'pnl_usdt': pnl_usdt_dummy,
            'pnl_rate': pnl_rate_dummy,
        }

    try:
        symbol = position['symbol']
        side_to_close = 'sell' if position['side'] == 'long' else 'buy' # 決済は逆方向
        amount_to_close = position['contracts'] # ポジションの数量

        # 決済注文 (成行注文)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', 
            side_to_close,
            amount_to_close, 
            params={'reduceOnly': True} # 利益確定/損切りのためのクローズ注文
        )

        if order and order['status'] in ['closed', 'filled']:
            
            # 約定価格 (決済価格)
            exit_price_final = order.get('price', 0.0) 
            
            # PnLの計算 (取引所から取得できない場合のため簡易計算)
            # 実際には CCXTの fetch_closed_orders などで詳細な PnL を取得すべきだが、簡略化のため...
            
            # 簡易PnL計算: (決済価格 - エントリー価格) / エントリー価格 * レバレッジ * ノミナルバリュー
            price_diff_rate = (exit_price_final - position['entry_price']) / position['entry_price']
            
            # ショートの場合は符号を反転
            if position['side'] == 'short':
                price_diff_rate = -price_diff_rate
                
            pnl_rate = price_diff_rate * LEVERAGE
            pnl_usdt = position['filled_usdt'] * pnl_rate
            
            return {
                'status': 'ok',
                'exit_type': exit_type,
                'entry_price': position['entry_price'],
                'exit_price': exit_price_final,
                'filled_amount': amount_to_close,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
            }
            
        else:
            return {'status': 'error', 'error_message': f"決済注文ステータス異常: {order.get('status', '不明')}"}


    except ccxt.ExchangeError as e:
        error_msg = f"取引所エラー (CCXT): {e}"
        logging.error(f"❌ ポジション決済失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ ポジション決済失敗: {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}
        
        
# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """
    メインの分析・取引ロジック。定期的に実行される。
    
    ⚠️ 注意: main_bot_schedulerでIS_CLIENT_READYがTrueであることを前提としています。
    """
    global CURRENT_MONITOR_SYMBOLS, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, OPEN_POSITIONS

    logging.info(f"--- メインボットループ実行開始 (JST: {datetime.now(JST).strftime('%H:%M:%S')}) ---")

    # 1. CCXTクライアントの初期化/再確認 (💡 schedulerに移譲したため、ここではスキップ)

    # 2. 口座ステータスの取得
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 口座ステータス取得失敗。取引をスキップします。")
        # エラー通知は fetch_account_status 内で行う
        return

    # 3. マクロコンテキスト (FGIなど) の取得
    macro_context = await get_macro_context()
    current_threshold = get_current_threshold(macro_context)
    
    # 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 監視対象銘柄リストを更新 (TOP銘柄のリストを取得)
        await update_current_monitor_symbols() 
        
        # ポジションを同期 (SL/TP監視ループとの同期のため)
        await fetch_open_positions()

        startup_msg = format_startup_message(account_status, macro_context, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_msg)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
        logging.info("✅ BOTサービス起動完了通知を送信しました。")


    # 4. 監視対象銘柄リストの更新 (定期的に実行)
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
         await update_current_monitor_symbols()
         LAST_HOURLY_NOTIFICATION_TIME = time.time()


    # 5. 全銘柄の分析とシグナル収集
    all_signals: List[Dict] = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # ポジションを保有している銘柄は、新規シグナルを発生させない (片張り戦略)
        is_position_open = any(pos['symbol'] == symbol for pos in OPEN_POSITIONS)
        if is_position_open:
            logging.info(f"ℹ️ {symbol}: ポジションを保有中のため、新規シグナル分析をスキップします。")
            continue
            
        signals = await analyze_symbol(symbol, macro_context)
        all_signals.extend(signals)
        await asyncio.sleep(0.01) # APIレートリミット対策

    # 6. スコアリングとトップシグナルの選定
    all_signals.sort(key=lambda x: x['score'], reverse=True)

    # フィルタリング: クールダウン期間、閾値、RRR
    tradable_signals = []
    
    # 最高のシグナルのみを処理
    for signal in all_signals:
        
        if len(tradable_signals) >= TOP_SIGNAL_COUNT:
            break

        symbol = signal['symbol']
        score = signal['score']
        
        # 閾値チェック
        if score < current_threshold:
            # 閾値以下で最高スコアだった場合、ログに残す
            if len(all_signals) > 0 and signal == all_signals[0] and time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
                 log_signal(signal, "低スコア・分析のみ", None)
                 # 通知は analysis_only_notification_scheduler で行う
                 
            continue 

        # クールダウンチェック (同一銘柄の取引シグナルから12時間経過しているか)
        last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        if (time.time() - last_trade_time) < TRADE_SIGNAL_COOLDOWN:
            logging.info(f"ℹ️ {symbol}: クールダウン期間 ({TRADE_SIGNAL_COOLDOWN/3600:.1f}h) 中のためスキップします。")
            continue
            
        # RRRチェック (1.5以上を必須とする)
        if signal['rr_ratio'] < 1.5:
             logging.info(f"ℹ️ {symbol}: RRR ({signal['rr_ratio']:.2f}) が低すぎるためスキップします。")
             continue
            
        tradable_signals.append(signal)
        
    
    LAST_ANALYSIS_SIGNALS = all_signals # 全シグナルを保持 (WebShare用)

    # 7. トップシグナルの取引実行
    if tradable_signals and not TEST_MODE:
        
        signal = tradable_signals[0] # 最もスコアが高いシグナルのみを処理
        symbol = signal['symbol']
        
        logging.info(f"🔥 **取引実行**: {symbol} ({signal['side'].upper()}) - スコア: {signal['score']:.4f}")
        
        # 取引実行
        trade_result = await execute_trade(signal)
        
        # 取引結果のログと通知
        log_signal(signal, "取引シグナル", trade_result)
        
        message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(message)
        
        # 成功した場合のみクールダウン時間を更新
        if trade_result.get('status') == 'ok':
            LAST_SIGNAL_TIME[symbol] = time.time()
            
            # ポジションがオープンした可能性があるので、ポジション監視リストを更新
            await fetch_open_positions()

    elif tradable_signals and TEST_MODE:
        # テストモードでのシグナル通知
        signal = tradable_signals[0]
        trade_result = {'status': 'test_mode', 'filled_usdt': FIXED_NOTIONAL_USDT}
        log_signal(signal, "テストシグナル", trade_result)
        message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(message)
        logging.info(f"ℹ️ **テストモード**: {signal['symbol']} の取引シグナルを検知しました (スコア: {signal['score']:.4f})")

    elif not tradable_signals:
        logging.info("ℹ️ 取引可能なシグナルはありませんでした。")
    
    
    # 8. WebShareログの送信 (定期的に)
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'bot_version': BOT_VERSION,
            'account_equity': ACCOUNT_EQUITY_USDT,
            'open_positions_count': len(OPEN_POSITIONS),
            'macro_context': macro_context,
            'top_signals': LAST_ANALYSIS_SIGNALS[:5], # スコアTOP5のみ
            'monitoring_symbols_count': len(CURRENT_MONITOR_SYMBOLS),
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = time.time()
        
    logging.info("--- メインボットループ実行終了 ---")


async def position_monitor_scheduler():
    """
    オープンポジションを定期的に監視し、SL/TPトリガー時に決済を試みる。
    """
    global OPEN_POSITIONS
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if not IS_CLIENT_READY:
            logging.warning("⚠️ ポジション監視スキップ: クライアント未準備")
            continue
            
        logging.debug("--- ポジション監視ループ実行開始 ---")
        
        try:
            # 1. CCXTから最新のオープンポジションを取得 (OPEN_POSITIONSを更新)
            # ポジション監視ロジックで使うのは、メインループが最後にフェッチしたリストだが、
            # 定期的に最新の状態に同期させておくことが望ましい。
            current_positions_ccxt = await fetch_open_positions()

            # 2. 各ポジションの現在価格を取得し、SL/TPをチェック
            for pos in current_positions_ccxt:
                symbol = pos['symbol']
                side = pos['side']
                
                # ポジション情報にSL/TPがない場合はスキップ
                if pos['stop_loss'] == 0.0 and pos['take_profit'] == 0.0:
                    logging.info(f"ℹ️ {symbol}: SL/TP未設定のポジションを監視しています。メインループがトリガーするのを待機。")
                    continue
                    
                # 最新の価格を取得 (ここではfetch_tickerを使用)
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                current_price = ticker['last']
                
                trigger_type = None
                
                if side == 'long':
                    # ロング: SL (下限) / TP (上限)
                    if current_price <= pos['stop_loss']:
                        trigger_type = 'SL'
                    elif current_price >= pos['take_profit']:
                        trigger_type = 'TP'
                        
                elif side == 'short':
                    # ショート: SL (上限) / TP (下限)
                    if current_price >= pos['stop_loss']:
                        trigger_type = 'SL'
                    elif current_price <= pos['take_profit']:
                        trigger_type = 'TP'

                
                # SL/TPがトリガーされた場合
                if trigger_type:
                    logging.warning(f"💥 **ポジション決済トリガー**: {symbol} ({side.upper()}) - {trigger_type} トリガー！ 価格: {current_price}")
                    
                    # 決済実行
                    trade_result = await close_position(pos, trigger_type)
                    
                    # 決済結果のログと通知
                    log_signal(pos, "ポジション決済", trade_result)
                    
                    # エントリー時のシグナル情報 (SL/TP情報) を使用してメッセージを整形
                    temp_signal_for_format = pos.copy()
                    temp_signal_for_format['stop_loss'] = pos['stop_loss']
                    temp_signal_for_format['take_profit'] = pos['take_profit']
                    temp_signal_for_format['rr_ratio'] = 0.0 # 決済時はRRRを再計算しない

                    # マクロコンテキストは最新のものを利用
                    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

                    message = format_telegram_message(temp_signal_for_format, "ポジション決済", current_threshold, trade_result)
                    await send_telegram_notification(message)
                    
                    # 決済完了後、ポジションリストを再フェッチし、クローズされたポジションを削除
                    await fetch_open_positions() 
                    
                await asyncio.sleep(0.01) # レートリミット対策

        except Exception as e:
            logging.error(f"❌ ポジション監視ループ中にエラーが発生しました: {e}", exc_info=True)
            
        logging.debug("--- ポジション監視ループ実行終了 ---")


async def analysis_only_notification_scheduler():
    """
    取引閾値未満だが、スコアが高いシグナルを定期的に通知する。
    """
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS

    while True:
        await asyncio.sleep(ANALYSIS_ONLY_INTERVAL)
        
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            continue
            
        current_time = time.time()
        
        # 最後に通知してから1時間以上経過しているか
        if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 閾値未満で最もスコアが高いシグナル (ただし、クールダウン期間中でないこと)
            top_analysis_signal: Optional[Dict] = None
            
            for signal in LAST_ANALYSIS_SIGNALS:
                if signal['score'] < current_threshold:
                     symbol = signal['symbol']
                     last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
                     if (current_time - last_trade_time) >= TRADE_SIGNAL_COOLDOWN:
                         top_analysis_signal = signal
                         break # 最初の (最高スコアの) 閾値未満シグナル
            
            if top_analysis_signal:
                
                trade_result_dummy = {'status': 'analysis_only', 'filled_usdt': 0.0}
                
                message = format_telegram_message(top_analysis_signal, "分析のみ", current_threshold, trade_result_dummy)
                
                analysis_msg = (
                    f"💡 **分析のみ通知**\n"
                    f"現在の市場環境 ({current_threshold*100:.0f}点) では取引できませんが、\n"
                    f"以下のシグナルが**次点**として検知されました:\n\n"
                )
                
                await send_telegram_notification(analysis_msg + message)
                
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time


async def main_bot_scheduler():
    """
    メインのボットループを定期的に実行するスケジューラ。
    """
    global LAST_SUCCESS_TIME
    
    # 初回起動時は即座に実行を試みる
    await asyncio.sleep(1) 
    
    while True:
        
        # 💡 【修正点】 CCXTクライアントが未初期化の場合、初期化を再試行するロジックをここに移動
        if not IS_CLIENT_READY:
            logging.info("ℹ️ CCXTクライアントが未初期化です。初期化を試行します...")
            
            # 初期化を試行し、失敗した場合はログを出力して待機
            if not await initialize_exchange_client():
                logging.critical("❌ クライアント再初期化に失敗。5秒待機後に再試行します。")
                
                # エラーがAPIキー/シークレット欠如による場合は通知
                if not API_KEY or not SECRET_KEY:
                     await send_telegram_notification(f"🚨 **致命的なエラー**\nCCXTクライアントの初期化に失敗しました: <code>APIキーまたはSECRET_KEYが不足しています。</code>")
                     
                await asyncio.sleep(5)
                continue
            else:
                # 成功したらログを出力
                logging.info("✅ クライアント初期化に成功しました。メインループに進みます。")


        current_time = time.time()
        
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: <code>{e}</code>")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME)) 
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


# ====================================================================================
# FASTAPI CONFIG & ENTRY POINT 
# ====================================================================================

# FastAPIアプリケーションインスタンス
app = FastAPI()

# ヘルスチェックエンドポイント (UptimeRobot用)
@app.get("/", status_code=200)
@app.head("/", status_code=200)
async def root():
    return {"status": "ok", "version": BOT_VERSION}


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())
    asyncio.create_task(analysis_only_notification_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request: Request, exc: Exception):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    # Unclosed client session などの軽微なエラーログを抑制
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {type(exc).__name__}"},
    )


if __name__ == "__main__":
    # 💡 実行時のファイル名に合わせて修正 (仮に 'main_render_24' とします)
    uvicorn.run("main_render__24:app", host="0.0.0.0", port=8000, log_config=None)
