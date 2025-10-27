# ====================================================================================
# Apex BOT v20.0.40 - Future Trading / 30x Leverage 
# (Feature: 固定取引ロット 20 USDT, 最小ロット堅牢性強化, MEXC 400/10007エラー対策)
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
BOT_VERSION = "v20.0.40-fix4"       # 💡 BOTバージョンを更新 (FIX適用)
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # 💡 FGI API URL

LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒) - 10秒ごと

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
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 # 12時間
TRADE_SIGNAL_LIQUIDITY_COOLDOWN = 60 * 60 * 24 # 💡 24時間 (流動性不足時の延長)

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
SIGNAL_THRESHOLD_SLUMP = 0.945       
SIGNAL_THRESHOLD_NORMAL = 0.90      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

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
    
    # SL比率 (通知用)
    sl_ratio = abs(entry_price - stop_loss) / entry_price if entry_price else 0.0


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
            risk_usdt = abs(filled_usdt_notional) * sl_ratio / (1/LEVERAGE) # 簡易的なSLによる名目リスク (リスク計算ロジックに依存)
            
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
            'defaultType': 'future', # 💡 defaultTypeをfutureに固定
        }
        
        # 💥 修正ポイント D: MEXC向けにシンボルID正規表現を緩和 (symbol not support api対策)
        if client_name == 'mexc':
             options['defaultType'] = 'swap' # MEXCでは 'swap' が先物に相当する場合が多い
             options['adjustForTimeDifference'] = True 
             # USDTで終わるものを全て先物として扱うことで、CCXTシンボルとMEXCシンボルIDの不一致を緩和
             options['futuresMarketRegex'] = 'USDT$' 
             
        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options, # 💡 修正後のオプションを適用
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
                         # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT) をリストに追加
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
                        'stop_loss': 0.0,
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
# ANALYTICAL CORE 
# ====================================================================================

def _calculate_ta_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたOHLCV DataFrameにテクニカル指標を適用する。
    """
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        return df
        
    # 指標の計算
    df[f'SMA{LONG_TERM_SMA_LENGTH}'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['RSI'] = ta.rsi(df['close'], length=14)
    # MACD (デフォルト設定: 12, 26, 9)
    macd_data = ta.macd(df['close'])
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9'] # ヒストグラム
    
    # ATR
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # OBV
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA20'] = ta.sma(df['OBV'], length=20)
    
    # PPO (Percentage Price Oscillator)のMACD (ボラティリティ測定用)
    df['PPO_HIST'] = df['MACDh'] # 簡易的なボラティリティ指標として使用
    
    return df

async def calculate_fgi() -> Dict:
    try:
        # 💡 FGI_API_URL を使用
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        # response.json() は {"data": [{...}], "metadata": {...}} の形式を想定
        data = response.json().get('data') 
        
        fgi_raw_value = int(data[0]['value']) if data and data[0]['value'] else 50
        fgi_classification = data[0]['value_classification'] if data and data[0]['value_classification'] else "Neutral"
        
        # FGIをスコアに変換: 0-100 -> -1.0 to 1.0 (例: 100=Greed=1.0, 0=Fear=-1.0)
        fgi_proxy = (fgi_raw_value / 50.0) - 1.0 
        
        return {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': f"{fgi_raw_value} ({fgi_classification})",
            'forex_bonus': 0.0 
        }
    except Exception as e:
        logging.error(f"❌ FGIの取得に失敗しました: {e}")
        return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A (APIエラー)', 'forex_bonus': 0.0}

async def get_top_volume_symbols(exchange: ccxt_async.Exchange, limit: int = TOP_SYMBOL_LIMIT, base_symbols: List[str] = DEFAULT_SYMBOLS) -> List[str]:
    """
    取引所から出来高トップの先物銘柄を動的に取得し、基本リストに追加する
    """
    
    logging.info(f"🔄 出来高トップ {limit} 銘柄の動的取得を開始します...")
    
    try:
        # 1. 全ティッカー情報（価格、出来高など）を取得
        tickers = await exchange.fetch_tickers()
        
        # 'NoneType' object has no attribute 'keys' のエラー対策 (Patch 75 Fix)
        if tickers is None or not isinstance(tickers, dict):
            logging.error(f"❌ {exchange.id}: fetch_tickersがNoneまたは無効なデータを返しました。デフォルトを使用します。")
            return base_symbols 

        volume_data = []
        
        for symbol, ticker in tickers.items():
            market = exchange.markets.get(symbol)
            
            # 1. 市場情報が存在し、アクティブであること
            if market is None or not market.get('active'):
                 continue

            # 2. Quote通貨がUSDTであり、取引タイプが先物/スワップであること (USDT-margined futures)
            if market.get('quote') == 'USDT' and market.get('type') in ['swap', 'future']:
                
                # 'quoteVolume' (引用通貨建て出来高 - USDT) を優先的に使用
                volume = ticker.get('quoteVolume')
                if volume is None:
                    # quoteVolumeがない場合、baseVolumeと最終価格で計算（概算）
                    base_vol = ticker.get('baseVolume')
                    last_price = ticker.get('last')
                    if base_vol is not None and last_price is not None:
                        volume = base_vol * last_price
                
                if volume is not None and volume > 0:
                    volume_data.append((symbol, volume))
        
        # 3. 出来高で降順にソートし、TOP N（40）のシンボルを抽出
        volume_data.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [s for s, v in volume_data[:limit]]
        
        # 4. デフォルトリストと結合し、重複を排除（動的取得できなかった場合も主要銘柄は維持）
        # 優先度の高いデフォルト銘柄を先頭に、出来高トップ銘柄を追加する形でリストを作成
        unique_symbols = list(base_symbols)
        for symbol in top_symbols:
            if symbol not in unique_symbols:
                unique_symbols.append(symbol)
        
        logging.info(f"✅ 出来高トップ銘柄を動的に取得しました (合計 {len(unique_symbols)} 銘柄)。")
        return unique_symbols

    except Exception as e:
        # エラーが発生した場合、デフォルトリストのみを返す (耐障害性の維持)
        # 💥 FIX: CCXT内部で発生したAttributeErrorのトレーサックを抑制 (exc_info=False)
        logging.error(f"❌ 出来高トップ銘柄の取得に失敗しました。デフォルト ({len(base_symbols)}件) を使用します: '{e}'", exc_info=False)
        return base_symbols

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    try:
        # ccxt.fetch_ohlcv を使用
        ohlcv_data = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # DataFrameに変換
        ohlcv = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ohlcv['timestamp'] = pd.to_datetime(ohlcv['timestamp'], unit='ms')
        
        if ohlcv.empty:
            raise Exception("OHLCV data is empty.")
            
        return ohlcv
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} {timeframe}: OHLCVデータの取得に失敗しました: {e}")
        return None

def apply_technical_analysis(symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Dict:
    """
    指定されたOHLCV DataFrameにテクニカル指標を適用し、分析結果を返す。
    """
    analyzed_data: Dict[str, Dict] = {}
    
    # 各時間足に指標を適用
    for tf, df in ohlcv.items():
        # SMA200の計算に必要な期間（約200期間）を確保できない場合はスキップ
        if not df.empty and len(df) >= LONG_TERM_SMA_LENGTH:
            analyzed_df = _calculate_ta_indicators(df)
            
            # 必須カラムのチェック (KeyError対策)
            required_cols = [f'SMA{LONG_TERM_SMA_LENGTH}', 'RSI', 'MACDh', 'ATR', 'OBV', 'OBV_SMA20']
            if not all(col in analyzed_df.columns for col in required_cols):
                 logging.warning(f"⚠️ {symbol} {tf}: 必須TA指標の計算に失敗しました (データ不足か計算エラー)。スキップ。")
                 continue
                 
            # 最終的な分析結果を抽出
            last = analyzed_df.iloc[-1] 
            
            # NaNチェック (NaNがあればその時間足の分析は無効)
            if any(pd.isna(last[col]) for col in required_cols):
                logging.warning(f"⚠️ {symbol} {tf}: 最新のTA結果にNaNが含まれています。スキップ。")
                continue
            
            # OBVの簡易トレンド: OBVがOBVのSMAを上回っているか
            obv_up = last['OBV'] > last['OBV_SMA20']
            
            analyzed_data[tf] = {
                'close': last['close'],
                'sma200': last[f'SMA{LONG_TERM_SMA_LENGTH}'],
                'rsi': last['RSI'],
                'macd_h': last['MACDh'],
                'atr': last['ATR'],
                'ppo_hist': last['MACDh'], # 簡易的なボラティリティ指標
                'obv_up': obv_up,
                'is_bull_trend': last['close'] > last[f'SMA{LONG_TERM_SMA_LENGTH}'],
                'is_bear_trend': last['close'] < last[f'SMA{LONG_TERM_SMA_LENGTH}'],
            }
        
    return analyzed_data

def calculate_signal_score(symbol: str, tech_signals: Dict, macro_context: Dict) -> Dict:
    """
    テクニカル分析の結果を統合し、最終的な複合シグナルスコアとSL/TP比率を計算する。
    """
    
    # メインの取引時間足と長期トレンド確認時間足
    main_tf = '1h'
    long_tf = '4h'
    
    if main_tf not in tech_signals or long_tf not in tech_signals:
        logging.warning(f"⚠️ {symbol}: 必要な時間足 ({main_tf}または{long_tf}) の分析データが不足しています。スコア0.0。")
        return {'score': 0.0, 'side': 'none', 'sl_ratio': 0.0, 'tp_ratio': 0.0, 'rr_ratio': 0.0, 'tech_data': {}}
        
    main_sig = tech_signals[main_tf]
    long_sig = tech_signals[long_tf]
    
    # 1. シグナル方向の決定 (ロング/ショート)
    # (4hトレンド + 1h MACD + 1h RSI) の賛成票で決定
    
    long_bias_score = 0
    short_bias_score = 0
    
    # 長期トレンド (4h SMA200)
    if long_sig['is_bull_trend']: long_bias_score += 1
    if long_sig['is_bear_trend']: short_bias_score += 1
        
    # メイン時間足のMACDモメンタム (ヒストグラムがプラス/マイナス)
    if main_sig['macd_h'] > 0: long_bias_score += 1
    if main_sig['macd_h'] < 0: short_bias_score += 1
        
    # メイン時間足のRSI (過熱感を避け、50付近でモメンタムを確認)
    if main_sig['rsi'] > 55 and main_sig['rsi'] < 70: long_bias_score += 1
    if main_sig['rsi'] < 45 and main_sig['rsi'] > 30: short_bias_score += 1
        
    if long_bias_score > short_bias_score:
        side = 'long'
    elif short_bias_score > long_bias_score:
        side = 'short'
    else:
        return {'score': 0.0, 'side': 'none', 'sl_ratio': 0.0, 'tp_ratio': 0.0, 'rr_ratio': 0.0, 'tech_data': {}}
        
    # 2. スコアリングの実行
    score = BASE_SCORE # 0.40 から開始
    tech_data = {
        'long_term_reversal_penalty_value': 0.0,
        'structural_pivot_bonus': 0.0,
        'macd_penalty_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'forex_bonus': 0.0, 
        'volatility_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0
    }
    
    # A. 長期トレンド一致の確認 (ボーナス/ペナルティ)
    trend_penalty_value = 0.0
    if side == 'long' and long_sig['is_bull_trend']:
        trend_penalty_value = LONG_TERM_REVERSAL_PENALTY
    elif side == 'short' and long_sig['is_bear_trend']:
        trend_penalty_value = LONG_TERM_REVERSAL_PENALTY
    elif side == 'long' and long_sig['is_bear_trend']:
        trend_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆行ペナルティ
    elif side == 'short' and long_sig['is_bull_trend']:
        trend_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆行ペナルティ
        
    score += trend_penalty_value
    tech_data['long_term_reversal_penalty_value'] = trend_penalty_value
    
    # B. MACDモメンタムの一致 (ボーナス/ペナルティ)
    macd_penalty_value = 0.0
    if side == 'long' and main_sig['macd_h'] > 0:
        macd_penalty_value = MACD_CROSS_PENALTY
    elif side == 'short' and main_sig['macd_h'] < 0:
        macd_penalty_value = MACD_CROSS_PENALTY
    elif side == 'long' and main_sig['macd_h'] < 0:
        macd_penalty_value = -MACD_CROSS_PENALTY
    elif side == 'short' and main_sig['macd_h'] > 0:
        macd_penalty_value = -MACD_CROSS_PENALTY

    score += macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # C. RSIモメンタム加速 (ボーナス)
    rsi_momentum_bonus = 0.0
    if side == 'long' and main_sig['rsi'] > 50:
        rsi_momentum_bonus = 0.05
    elif side == 'short' and main_sig['rsi'] < 50:
        rsi_momentum_bonus = 0.05
    score += rsi_momentum_bonus
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus

    # D. OBV出来高確証 (ボーナス)
    obv_momentum_bonus = 0.0
    if side == 'long' and main_sig['obv_up']:
        obv_momentum_bonus = OBV_MOMENTUM_BONUS
    elif side == 'short' and not main_sig['obv_up']:
        obv_momentum_bonus = OBV_MOMENTUM_BONUS
    score += obv_momentum_bonus
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus
    
    # E. 構造的優位性 (固定ボーナス/ベースライン)
    score += STRUCTURAL_PIVOT_BONUS
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
    
    # F. 流動性ボーナス (トップ銘柄ほどスコアアップ)
    if symbol in CURRENT_MONITOR_SYMBOLS:
        try:
            rank = CURRENT_MONITOR_SYMBOLS.index(symbol) + 1
            liquidity_bonus = LIQUIDITY_BONUS_MAX * (1 - (rank / (TOP_SYMBOL_LIMIT * 2)))
            liquidity_bonus = max(0.0, min(LIQUIDITY_BONUS_MAX, liquidity_bonus))
            score += liquidity_bonus
            tech_data['liquidity_bonus_value'] = liquidity_bonus
        except ValueError:
            pass # デフォルトシンボルに含まれていなければ、ボーナスなし
    
    # G. マクロ要因 (FGI) ボーナス/ペナルティ
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_bonus = fgi_proxy * (FGI_PROXY_BONUS_MAX / 1.0) # -1.0から1.0のFGIに最大影響度を乗算
    
    if side == 'long':
        fgi_influence = max(0.0, sentiment_bonus) # ロングはGreed/Positiveのみボーナス
    elif side == 'short':
        fgi_influence = min(0.0, sentiment_bonus) # ショートはFear/Negativeのみボーナス/ペナルティ
    else:
        fgi_influence = 0.0
        
    score += fgi_influence
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_influence
    
    # H. ボラティリティによるペナルティ
    # ATR/Priceで相対ボラティリティを計算
    current_price = main_sig['close']
    current_atr = main_sig['atr']
    relative_volatility = current_atr / current_price 
    
    volatility_penalty = 0.0
    # 例: 過去のボラティリティと比較して過熱しすぎている場合にペナルティ
    # ここでは簡易的に、一定の相対ボラティリティ（例: 0.8%）を超えた場合にペナルティ
    if relative_volatility > VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 0.01 = 1%
        volatility_penalty = -0.05
        
    score += volatility_penalty
    tech_data['volatility_penalty_value'] = volatility_penalty

    # 3. リスク・リワード比率 (RRR) の計算と調整
    # SL幅をATRの倍率で設定
    sl_ratio = (current_atr * ATR_MULTIPLIER_SL) / current_price # ATRベースのSL%
    sl_ratio = max(MIN_RISK_PERCENT, sl_ratio) # 最小リスク幅の適用
    
    # RRRの決定: スコアに応じてTP/SLの比率を動的に調整
    # スコアが高いほどRRRを大きくする (リスクを固定し、リワードを大きくする)
    # 例: Score 0.65 -> RRR 1.5, Score 0.90 -> RRR 2.5
    
    # RRR = 1.0 + (スコア - 最小スコア) / (最大スコア - 最小スコア) * 最大RRR変動
    min_score_for_trade = get_current_threshold(macro_context)
    base_rr = 1.2
    max_rr_bonus = 1.5
    
    if score >= min_score_for_trade:
        rr_ratio = base_rr + ((score - min_score_for_trade) / (1.0 - min_score_for_trade)) * max_rr_bonus
        rr_ratio = round(rr_ratio, 2)
    else:
        rr_ratio = base_rr
        
    # SL幅に対するTP幅のパーセンテージ
    tp_ratio = sl_ratio * rr_ratio 

    # 4. 最終スコアのクランプ (0.0から1.0の範囲に制限)
    final_score = max(0.0, min(1.0, score))
    
    return {
        'score': final_score,
        'side': side,
        'timeframe': main_tf,
        'sl_ratio': sl_ratio,
        'tp_ratio': tp_ratio,
        'rr_ratio': rr_ratio,
        'current_price': current_price,
        'tech_data': tech_data
    }

def _calculate_risk_and_trade_size(signal: Dict) -> Dict:
    """
    シグナルに基づいて、固定名目価値、SL/TP価格、およびロットサイズを計算する。
    """
    
    side = signal['side']
    current_price = signal['current_price']
    sl_ratio = signal['sl_ratio']
    tp_ratio = signal['tp_ratio']
    
    # A. SL/TP価格の計算
    if side == 'long':
        stop_loss_price = current_price * (1.0 - sl_ratio)
        take_profit_price = current_price * (1.0 + tp_ratio)
        
    elif side == 'short':
        stop_loss_price = current_price * (1.0 + sl_ratio)
        take_profit_price = current_price * (1.0 - tp_ratio)
        
    else:
        return {'error': 'Invalid side'}

    # B. 清算価格の計算
    liquidation_price = calculate_liquidation_price(
        entry_price=current_price, 
        leverage=LEVERAGE, 
        side=side,
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )
    
    # C. 取引ロットサイズ (固定名目価値から計算)
    # 必要名目価値 = FIXED_NOTIONAL_USDT (20 USDT)
    
    # ロットサイズ (基本通貨単位, 例: BTC) = 名目価値 / 価格
    # 🚨 注意: これはまだ取引所の精度に丸められていない生の値
    lot_size_raw = FIXED_NOTIONAL_USDT / current_price 
    
    return {
        'lot_size_raw': lot_size_raw,
        'notional_usdt': FIXED_NOTIONAL_USDT,
        'entry_price': current_price,
        'stop_loss': stop_loss_price,
        'take_profit': take_profit_price,
        'liquidation_price': liquidation_price,
    }

async def execute_trade_logic(signal: Dict, risk_data: Dict) -> Dict:
    """
    実際に取引所に注文を送信し、結果を返す。
    """
    global EXCHANGE_CLIENT
    symbol = signal['symbol']
    side = signal['side']
    
    # 注文に必要なデータ
    amount_raw = risk_data['lot_size_raw']
    notional_usdt = risk_data['notional_usdt']
    stop_loss = risk_data['stop_loss']
    take_profit = risk_data['take_profit']
    entry_price = risk_data['entry_price']
    
    # 注文方向
    ccxt_side = 'buy' if side == 'long' else 'sell'
    # 注文タイプ (指値ではなく成行を推奨)
    order_type = 'market' 
    
    if TEST_MODE:
        return {
            'status': 'ok',
            'filled_amount': amount_raw,
            'filled_usdt': notional_usdt,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'error_message': 'TEST_MODE'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
         return {'status': 'error', 'error_message': 'CCXT Client is not ready.'}

    # 1. 市場情報とロットサイズ/価格の精度を取得
    market_info = EXCHANGE_CLIENT.markets.get(symbol)
    if not market_info:
        return {'status': 'error', 'error_message': f'Market info not found for {symbol}.'}

    # ロットサイズ/価格の丸め
    try:
        # amount_to_precision: ロットサイズを取引所が許容する最小単位に丸める
        amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_raw)
        amount_adjusted = float(amount_adjusted_str)
        
        # price_to_precision: SL/TP価格を取引所が許容する価格単位に丸める
        sl_adjusted_str = EXCHANGE_CLIENT.price_to_precision(symbol, stop_loss)
        sl_adjusted = float(sl_adjusted_str)
        
        tp_adjusted_str = EXCHANGE_CLIENT.price_to_precision(symbol, take_profit)
        tp_adjusted = float(tp_adjusted_str)

        min_amount_adjusted = float(EXCHANGE_CLIENT.amount_to_precision(symbol, market_info['limits']['amount']['min']))
        
    except Exception as e:
        return {'status': 'error', 'error_message': f'Lot/Price precision calculation failed: {e}'}

    # 2. ロットサイズ/価格の妥当性チェック
    if amount_adjusted <= 0.0:
        return {'status': 'error', 'error_message': f'Calculated lot size is zero or too small ({amount_adjusted:.8f})'}
        
    # 💥 修正ポイント A: 精度調整の結果、0または最小ロットを下回った場合の強制フォールバック
    if amount_adjusted <= 0.0 or amount_adjusted < min_amount_adjusted * 0.9999999:
        # 強制的に最小ロットの1.1倍を適用
        amount_adjusted = min_amount_adjusted * 1.1 
        # 再度、精度調整を行う
        amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_adjusted)
        amount_adjusted = float(amount_adjusted_str)
        
        if amount_adjusted <= 0.0:
            logging.critical(f"❌ {symbol} 注文実行エラー: ロットサイズの強制再調整後も数量 ({amount_adjusted:.8f}) が0以下になりました。")
            return {'status': 'error', 'error_message': 'Amount rounded down to zero even after forced adjustment.'}
            
        logging.warning(f"⚠️ {symbol}: 精度調整後の数量 ({amount_adjusted_str}) が最小ロットを下回ったため、最小ロットの1.1倍に**再調整**しました。")
        
    
    # 💥 修正ポイント C: MEXC 400エラー対策 - 注文前のロットサイズの最終確認と強制再調整 (最小名目価値)
    # 最小名目価値（MEXCでは最低取引額/Cost）が設定されているか確認
    min_notional_value = market_info.get('limits', {}).get('cost', {}).get('min', 1.0) 
    current_notional_value = amount_adjusted * entry_price
    
    if current_notional_value < min_notional_value * 0.999: 
        # 最小名目価値を満たすために必要なロット数を計算し、強制的に適用
        amount_required_for_min_notional = min_notional_value * 1.05 / entry_price # 5%の余裕を持たせる
        amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_required_for_min_notional)
        amount_adjusted = float(amount_adjusted_str)
        
        logging.warning(
            f"⚠️ {symbol}: 最終ロット ({format_usdt(current_notional_value)} USDT) が取引所の最小名目価値 ({format_usdt(min_notional_value)} USDT) を下回るため、"
            f"ロットを最小名目価値ベースで再調整しました。最終数量: {amount_adjusted_str}"
        )
        # 再調整後の名目価値を更新
        notional_usdt = amount_adjusted * entry_price
        
    logging.info(f"✅ {symbol}: 最終ロットサイズ {amount_adjusted_str} (最小ロット: {min_amount_adjusted:.8f}). 名目価値: {format_usdt(notional_usdt)} USDT (固定).")

    # 3. 注文の実行
    order = None
    try:
        # 成行注文を送信
        # params={'position_side': side.upper()} # 必要に応じてポジションサイドを指定 (MEXCは不要な場合あり)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            order_type,
            ccxt_side,
            amount_adjusted,
            params={'leverage': LEVERAGE, 'clientOrderId': f'APEX-{uuid.uuid4().hex[:12]}'} 
        )
        
        # 注文が成功した場合、SL/TPのoco注文 (または単一注文) を実行
        # MEXCの場合、注文にSL/TPパラメータを含める必要がある
        # ccxtのcreateOrderでは、通常、SL/TPは個別のAPIコールになるため、ここでは省略
        # ポジション監視ループ (monitor_and_manage_positions) で、ポジション確立後にSL/TPを設定する
        
        # 注文の結果から約定情報を抽出
        filled_amount = order.get('filled', amount_adjusted)
        final_entry_price = order.get('price', entry_price) # 注文価格 (成行の場合、約定価格)
        
        # ポジション管理用のデータを作成 (ポジション監視ループで利用)
        new_position_data = {
            'symbol': symbol,
            'side': side,
            'entry_price': final_entry_price,
            'contracts': filled_amount,
            'filled_usdt': filled_amount * final_entry_price,
            'stop_loss': sl_adjusted,
            'take_profit': tp_adjusted,
            'timestamp': int(time.time() * 1000)
        }
        
        # グローバルなポジションリストに追加
        OPEN_POSITIONS.append(new_position_data)
        
        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_amount * final_entry_price,
            'entry_price': final_entry_price,
            'stop_loss': sl_adjusted,
            'take_profit': tp_adjusted,
            'order_id': order.get('id'),
            'error_message': None
        }

    except ccxt.ExchangeError as e:
        # CCXTエラーコードから、ロットサイズがゼロまたは小さすぎたエラーを特定
        is_lot_size_error = '10007' in str(e) or 'symbol not support api' in str(e) or '400' in str(e) or 'too small' in str(e).lower() or 'zero' in str(e).lower()
        log_msg = f"❌ {symbol} 注文実行エラー: {EXCHANGE_CLIENT.id} {e}"
        logging.error(log_msg)
        
        return {
            'status': 'error', 
            'error_message': log_msg,
            'filled_amount': amount_adjusted,
            'filled_usdt': notional_usdt
        }
    except Exception as e:
        log_msg = f"❌ {symbol} 注文実行エラー: ロット修正を試行しましたが失敗。{e}"
        logging.error(log_msg)
        return {
            'status': 'error', 
            'error_message': log_msg,
            'filled_amount': amount_adjusted,
            'filled_usdt': notional_usdt
        }

async def close_position(position: Dict, exit_type: str) -> Dict:
    """
    指定されたポジションを成行で決済し、結果を返す。
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    
    # 決済方向は、ポジションの反対側
    close_side = 'sell' if side == 'long' else 'buy'
    
    if TEST_MODE:
        logging.info(f"⚠️ TEST_MODE: {symbol} {side} ポジションを {exit_type} で決済シミュレート。")
        return {
            'status': 'ok',
            'exit_type': exit_type,
            'entry_price': position['entry_price'],
            'exit_price': position['entry_price'] * (1.02 if exit_type == 'TP' else 0.98), # 簡易P/Lシミュレーション
            'pnl_usdt': 5.0 if exit_type == 'TP' else -5.0,
            'pnl_rate': 0.1 if exit_type == 'TP' else -0.1,
            'filled_amount': contracts,
            'error_message': 'TEST_MODE'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
         return {'status': 'error', 'error_message': 'CCXT Client is not ready.'}
         
    # 1. ロットサイズの丸め (全量決済を意図)
    try:
        amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, contracts)
        amount_adjusted = float(amount_adjusted_str)
    except Exception as e:
        return {'status': 'error', 'error_message': f'Lot precision calculation failed for closing: {e}'}


    # 2. 決済注文の実行
    order = None
    try:
        # 成行注文でポジションを閉じる (params={'reduceOnly': True} を推奨)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market',
            close_side,
            amount_adjusted,
            params={'reduceOnly': True} 
        )
        
        # 注文の結果から約定情報を抽出
        filled_amount = order.get('filled', amount_adjusted)
        exit_price = order.get('price', 0.0) # 注文価格 (成行の場合、約定価格)
        
        if filled_amount == 0.0 or exit_price == 0.0:
            raise Exception("No fill or zero exit price detected.")
            
        # PNL計算 (簡易)
        pnl_rate_raw = (exit_price / position['entry_price'] - 1.0) * (1.0 if side == 'long' else -1.0) * LEVERAGE
        pnl_usdt_raw = position['filled_usdt'] * pnl_rate_raw 

        return {
            'status': 'ok',
            'exit_type': exit_type,
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt_raw,
            'pnl_rate': pnl_rate_raw / LEVERAGE, # レバレッジを除いた変動率
            'filled_amount': filled_amount,
            'error_message': None
        }

    except ccxt.ExchangeError as e:
        log_msg = f"❌ {symbol} 決済実行エラー: {EXCHANGE_CLIENT.id} {e}"
        logging.error(log_msg)
        return {
            'status': 'error', 
            'error_message': log_msg,
            'filled_amount': amount_adjusted,
            'exit_price': position.get('entry_price', 0.0)
        }
    except Exception as e:
        log_msg = f"❌ {symbol} 決済実行エラー: 予期せぬエラー: {e}"
        logging.error(log_msg)
        return {
            'status': 'error', 
            'error_message': log_msg,
            'filled_amount': amount_adjusted,
            'exit_price': position.get('entry_price', 0.0)
        }


# ====================================================================================
# POSITION MONITORING & MANAGEMENT 
# ====================================================================================

async def monitor_and_manage_positions() -> None:
    """
    オープンポジションのSL/TPを監視し、トリガーされた場合は決済処理を実行する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"🕵️‍♂️ {len(OPEN_POSITIONS)} 銘柄のポジション監視を開始します。")

    # 1. 全監視銘柄の最新価格を一括取得
    symbols_to_fetch = [p['symbol'] for p in OPEN_POSITIONS]
    try:
        # MEXCはfetch_tickersで全銘柄の価格を同時に取得できる (レートリミット対策)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch) 
    except Exception as e:
        logging.error(f"❌ ポジション監視中の価格取得に失敗しました: {e}")
        return

    # 2. SL/TPチェックと動的なSL/TP設定
    positions_to_close: List[Tuple[Dict, str]] = []
    positions_to_remove: List[Dict] = []
    
    for i, position in enumerate(OPEN_POSITIONS):
        symbol = position['symbol']
        side = position['side']
        entry_price = position['entry_price']
        
        # 最新の価格を取得
        ticker = tickers.get(symbol)
        if not ticker or not ticker.get('last'):
            logging.warning(f"⚠️ {symbol}: 最新の価格情報が取得できませんでした。このポジションの監視をスキップします。")
            continue
            
        current_price = ticker['last']
        
        # **A. SL/TPが未設定の場合の初期設定**
        if position['stop_loss'] == 0.0 or position['take_profit'] == 0.0:
            # 既にポジションが確立されているため、新規シグナルで計算したSL/TPを適用する。
            # 必要なOHLCVを取得し、シグナル再計算を行う必要があるが、
            # 複雑化を避けるため、ここでは簡易的に ATR を再取得して計算する。
            
            # 簡易的な価格変動率 SL=0.8%, TP=1.6% の比率で計算 (リスクを固定)
            default_sl_ratio = 0.008 # 0.8% 
            default_tp_ratio = default_sl_ratio * 2.0 # RRR 2.0
            
            if side == 'long':
                position['stop_loss'] = entry_price * (1.0 - default_sl_ratio)
                position['take_profit'] = entry_price * (1.0 + default_tp_ratio)
            else:
                position['stop_loss'] = entry_price * (1.0 + default_sl_ratio)
                position['take_profit'] = entry_price * (1.0 - default_tp_ratio)
                
            logging.info(f"ℹ️ {symbol}: SL/TP未設定のため、初期値 ({default_sl_ratio*100:.2f}% / {default_tp_ratio*100:.2f}%) を設定しました。")
            
            
        sl_price = position['stop_loss']
        tp_price = position['take_profit']
        
        # **B. SL/TPトリガーチェック**
        if side == 'long':
            # ロング: 価格がSLを下回る、またはTPを上回る
            if current_price <= sl_price:
                positions_to_close.append((position, 'SL (Stop Loss)'))
            elif current_price >= tp_price:
                positions_to_close.append((position, 'TP (Take Profit)'))
                
        elif side == 'short':
            # ショート: 価格がSLを上回る、またはTPを下回る
            if current_price >= sl_price:
                positions_to_close.append((position, 'SL (Stop Loss)'))
            elif current_price <= tp_price:
                positions_to_close.append((position, 'TP (Take Profit)'))
                
        # ⚠️ 注: マニュアル決済（BOT外）や清算（Liquidation）は、fetch_open_positionsで自動的にリストから削除される。


    # 3. 決済処理の実行 (非同期で実行)
    for position, exit_type in positions_to_close:
        symbol = position['symbol']
        
        logging.warning(f"🚨 {symbol} のポジションが {exit_type} をトリガーしました。決済を試行します。")
        
        trade_result = await close_position(position, exit_type)
        
        if trade_result['status'] == 'ok':
            # 決済成功: ログと通知を送信し、グローバルリストから削除
            log_signal(position, "ポジション決済", trade_result)
            
            # 通知用に最終P/Lを反映
            notification_data = {
                **position, 
                'score': 0.0, # 決済時はスコアは不要
                'rr_ratio': 0.0,
                'timeframe': 'N/A',
            }
            await send_telegram_notification(format_telegram_message(
                notification_data, 
                "ポジション決済", 
                get_current_threshold(GLOBAL_MACRO_CONTEXT),
                trade_result=trade_result,
                exit_type=exit_type
            ))
            
            # 決済されたポジションをグローバルリストから安全に削除するために一時リストに追加
            positions_to_remove.append(position)
            
        else:
            logging.error(f"❌ {symbol} の決済に失敗しました: {trade_result['error_message']}")


    # 4. 決済完了したポジションをグローバルリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
    
    if positions_to_close:
        # 再度最新のポジションリストを取得し、決済が反映されていることを確認
        await fetch_open_positions()
        
    logging.info(f"✅ ポジション監視/決済処理を完了しました。残存ポジション: {len(OPEN_POSITIONS)}")

# ====================================================================================
# MAIN BOT EXECUTION 
# ====================================================================================

async def main_bot_loop() -> None:
    """
    ボットのメイン実行ロジック。定期的に実行される。
    """
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS
    global LAST_SIGNAL_TIME, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED
    
    logging.info("⚙️ メインループを開始します。")

    # 1. 口座ステータスの更新
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 致命的エラー: 口座ステータスの取得に失敗しました。取引をスキップします。")
        return

    # 2. 出来高トップ銘柄の動的取得 (初回のみ、または定期的に更新)
    if not IS_FIRST_MAIN_LOOP_COMPLETED or not SKIP_MARKET_UPDATE:
        CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols(EXCHANGE_CLIENT, TOP_SYMBOL_LIMIT, DEFAULT_SYMBOLS)
        # 定期的な市場情報更新のフラグ設定をスキップする場合はコメントアウト

    # 3. オープンポジションの更新
    await fetch_open_positions()

    # 4. 市場環境スコア (FGI) の取得と動的閾値の計算
    GLOBAL_MACRO_CONTEXT = await calculate_fgi()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    fgi_raw = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    logging.info(f"📊 市場環境スコア: FGI {fgi_raw}。動的取引閾値: {current_threshold*100:.2f} / 100")

    # 5. 全銘柄の分析とシグナル生成
    all_signals: List[Dict] = []
    
    # 全ての監視銘柄に対して非同期で処理を実行
    analysis_tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        analysis_tasks.append(asyncio.create_task(process_symbol_analysis(symbol)))
        
    analysis_results = await asyncio.gather(*analysis_tasks)
    
    for result in analysis_results:
        if result and result.get('score', 0.0) > 0.0:
            all_signals.append(result)

    # 6. スコアの高いシグナルをフィルタリング
    # スコアで降順にソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # 閾値を超えたシグナルのみを抽出
    filtered_signals = [s for s in all_signals if s['score'] >= current_threshold]
    
    # 7. クールダウン期間のチェックとフィルタリング
    executable_signals: List[Dict] = []
    current_time = time.time()
    
    for signal in filtered_signals:
        symbol = signal['symbol']
        
        # クールダウン期間をチェック
        last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        cooldown_period = TRADE_SIGNAL_COOLDOWN
        
        # 出来高が低い銘柄はクールダウン期間を延長
        # if signal.get('tech_data', {}).get('liquidity_bonus_value', 0.0) < LIQUIDITY_BONUS_MAX * 0.5:
        #     cooldown_period = TRADE_SIGNAL_LIQUIDITY_COOLDOWN
            
        if current_time - last_trade_time > cooldown_period:
            # ポジションを保有していない銘柄のみを対象とする
            if not any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                executable_signals.append(signal)
            else:
                logging.info(f"ℹ️ {symbol}: ポジションを保有中のためスキップします。")
        else:
            logging.info(f"ℹ️ {symbol}: クールダウン期間中のためスキップします (残り {int((cooldown_period - (current_time - last_trade_time)) / 60)} 分)。")

    
    # 8. 取引の実行 (最もスコアの高いシグナル)
    
    # ログ出力用の情報
    top_signal_info = all_signals[0]['symbol'] + f" - スコア: {all_signals[0]['score'] * 100:.2f}" if all_signals else 'N/A'
    logging.info(f"📈 検出シグナル: {len(all_signals)} 銘柄。取引閾値 ({current_threshold*100:.2f}) を超えたシグナル: {len(filtered_signals)} 銘柄。 (トップ: {top_signal_info})")

    if not TEST_MODE and executable_signals:
        
        # 最もスコアの高いものを選択
        top_executable_signal = executable_signals[0] 
        symbol_to_trade = top_executable_signal['symbol']
        side_to_trade = top_executable_signal['side']
        
        logging.info(f"🔥 強力なシグナル検出 (実行対象): {symbol_to_trade} - {side_to_trade.upper()} (Score: {top_executable_signal['score'] * 100:.2f})")

        # リスクとロットサイズの計算
        risk_data = _calculate_risk_and_trade_size(top_executable_signal)
        
        # ログにSL/TP/Liq Priceを追記
        top_executable_signal.update(risk_data) 
        
        # 取引実行
        trade_result = await execute_trade_logic(top_executable_signal, risk_data)

        # 取引シグナルログの記録
        log_signal(top_executable_signal, "取引シグナル", trade_result)
        
        if trade_result['status'] == 'ok':
            # 成功した場合、クールダウン期間を更新
            LAST_SIGNAL_TIME[symbol_to_trade] = current_time
            # ログの情報を通知用に更新
            top_executable_signal.update({
                 'entry_price': trade_result['entry_price'], 
                 'stop_loss': trade_result['stop_loss'],
                 'take_profit': trade_result['take_profit'],
            }) 
            
        # Telegram通知
        await send_telegram_notification(format_telegram_message(
            top_executable_signal, 
            "取引シグナル", 
            current_threshold,
            trade_result=trade_result
        ))
        
    else:
        if TEST_MODE and filtered_signals:
            top_signal = filtered_signals[0]
            logging.info(f"⚠️ TEST MODE: {top_signal['symbol']} ({top_signal['side'].upper()}) が取引閾値を超えましたが、実行をスキップしました。")
            
        elif not filtered_signals and all_signals:
             # シグナルはあったが閾値超えなしの場合
            logging.info(f"ℹ️ 検出シグナルはありましたが、取引閾値 ({current_threshold*100:.2f}) を超えるものはありませんでした。")
            
        elif not all_signals:
            logging.info("ℹ️ 今回のループで有効な取引シグナルは検出されませんでした。")
            
    # 9. WebShareデータ送信 (定期的)
    global LAST_WEBSHARE_UPLOAD_TIME
    if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'bot_version': BOT_VERSION,
            'exchange': CCXT_CLIENT_NAME.upper(),
            'test_mode': TEST_MODE,
            'account_equity_usdt': ACCOUNT_EQUITY_USDT,
            'open_positions_count': len(OPEN_POSITIONS),
            'top_signals': all_signals[:5],
            'macro_context': GLOBAL_MACRO_CONTEXT,
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = current_time
            
    # 初回ループ完了フラグ
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        await send_telegram_notification(format_startup_message(
            account_status, 
            GLOBAL_MACRO_CONTEXT, 
            len(CURRENT_MONITOR_SYMBOLS), 
            current_threshold
        ))
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    logging.info(f"✅ メインループを完了しました。")

async def process_symbol_analysis(symbol: str) -> Optional[Dict]:
    """個々の銘柄のOHLCVを取得し、分析とスコアリングを実行するヘルパー関数"""
    
    ohlcv_data: Dict[str, pd.DataFrame] = {}
    
    # 1. 必要なOHLCVデータを非同期で取得
    fetch_tasks = []
    for tf in TARGET_TIMEFRAMES:
        fetch_tasks.append(fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf]))
        
    ohlcv_results = await asyncio.gather(*fetch_tasks)
    
    for tf, df in zip(TARGET_TIMEFRAMES, ohlcv_results):
        if df is not None:
            ohlcv_data[tf] = df
            
    if not ohlcv_data:
        logging.warning(f"⚠️ {symbol}: 必要なOHLCVデータが取得できませんでした。分析をスキップします。")
        return None
        
    # 2. テクニカル分析の実行
    tech_signals = apply_technical_analysis(symbol, ohlcv_data)
    
    # 3. シグナルスコアの計算
    signal_result = calculate_signal_score(symbol, tech_signals, GLOBAL_MACRO_CONTEXT)
    
    if signal_result['score'] > 0.0:
        return {
            'symbol': symbol,
            **signal_result
        }
    return None

async def main_bot_scheduler() -> None:
    """
    メインループとポジション監視ループを定期的にスケジュールする。
    """
    global LAST_SUCCESS_TIME
    
    await initialize_exchange_client()
    
    if not IS_CLIENT_READY:
         logging.critical("❌ CCXTクライアントが初期化できませんでした。ボットを終了します。")
         return 

    # 初回実行
    await main_bot_loop()
    LAST_SUCCESS_TIME = time.time()
    
    # スケジューリングループ
    while True:
        try:
            current_time = time.time()
            
            # 1. ポジション監視ループ (10秒ごと)
            await monitor_and_manage_positions()
            
            # 2. メイン取引ループ (60秒ごと)
            time_to_wait = LOOP_INTERVAL - (current_time - LAST_SUCCESS_TIME)
            
            if time_to_wait <= 0:
                await main_bot_loop()
                LAST_SUCCESS_TIME = time.time()
                time_to_wait = LOOP_INTERVAL
            
            logging.info(f"(main_bot_scheduler) - 次のメインループまで {time_to_wait:.1f} 秒待機します。")
            
            # 待機時間は、メインループとポジション監視ループの最小間隔に合わせる
            wait_for_next = min(MONITOR_INTERVAL, max(1, math.ceil(time_to_wait)))
            await asyncio.sleep(wait_for_next) 

        except Exception as e:
            logging.critical(f"❌ 致命的なスケジューラエラー: {e}", exc_info=True)
            # 致命的なエラーが発生した場合、クライアントを再初期化して再試行
            await initialize_exchange_client()
            await asyncio.sleep(60) # 60秒待機して再試行

# ====================================================================================
# FASTAPI ENDPOINTS & LIFESPAN
# ====================================================================================

app = FastAPI(title="Apex Trading Bot API", version=BOT_VERSION)
bot_task: Optional[asyncio.Task] = None

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にボットスケジューラを開始"""
    global bot_task
    # メインのボットロジックを非同期タスクとして起動
    logging.info("🚀 FastAPI起動イベント: ボットスケジューラを開始します。")
    bot_task = asyncio.create_task(main_bot_scheduler())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時にボットとCCXTクライアントを停止"""
    global bot_task, EXCHANGE_CLIENT
    if bot_task:
        bot_task.cancel()
        logging.info("🛑 FastAPIシャットダウンイベント: ボットスケジューラをキャンセルしました。")
    
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ CCXTクライアントをクローズしました。")
        except Exception as e:
            logging.error(f"❌ CCXTクライアントのクローズ中にエラー: {e}")

@app.get("/")
async def root():
    """ルートエンドポイント - BOTの基本情報と現在の状態を返す"""
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    response_data = {
        "status": "Running" if bot_task and not bot_task.done() else "Stopped/Failed",
        "bot_version": BOT_VERSION,
        "exchange": CCXT_CLIENT_NAME.upper(),
        "test_mode": TEST_MODE,
        "current_time_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "account_equity_usdt": format_usdt(ACCOUNT_EQUITY_USDT),
        "open_positions_count": len(OPEN_POSITIONS),
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "trade_threshold_score": f"{current_threshold*100:.2f} / 100",
        "fgi_score": GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A'),
    }
    return JSONResponse(content=response_data)

@app.head("/status")
async def status_check():
    """ヘルスチェックエンドポイント (HEADリクエスト用)"""
    if bot_task and not bot_task.done():
        return Response(status_code=200)
    return Response(status_code=503)

# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    # 環境変数またはデフォルト値からホストとポートを取得
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", 8080))

    # UvicornでFastAPIアプリケーションを起動
    # reload=True は開発環境向け。本番環境では外す。
    try:
        logging.info(f"🚀 Uvicornサーバーを起動します (http://{host}:{port})")
        uvicorn.run("main_render:app", host=host, port=port, log_level="info", reload=False)
    except Exception as e:
        logging.critical(f"❌ Uvicornサーバーの起動に失敗しました: {e}")
        sys.exit(1)
