# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 2. 注文失敗エラー (Amount can not be less than zero) 対策
# 3. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 4. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
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
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.43"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 

LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 5    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

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
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 

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
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 順張りボーナス/逆張りペナルティの値
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              # RSI 40/60をモメンタム加速の閾値に使用
MACD_CROSS_PENALTY = 0.15          # MACDの方向一致ボーナス/不一致ペナルティ
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             # SL = 2.0 * ATR
MIN_RISK_PERCENT = 0.008            # 最低リスク幅 (0.8%) 
RR_RATIO_TARGET = 1.5               # 💡 新規追加: 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
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
        
    return max(0.0, liquidation_price) 

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
        f"  - **取引ロット**: **固定** <code>{format_usdt(FIXED_NOTIONAL_USDT)}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **ATRベース**の**動的SL**で管理\n" 
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
        f"  - **FGI (恐怖・貪欲)**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
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
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得 (process_entry_signalで更新されているはず)
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        
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
            # 💡 修正: リスク額はSL比率とレバレッジで計算
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
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
            # ポジションをクローズするため、契約数は元のポジションの契約数を反映させるべき
            if 'contracts' in signal:
                 filled_amount = signal['contracts'] 
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
        f"  - **取引閾値**: <code>{current_threshold * 100:.0f}</code> 点\n"
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
            f"{get_score_breakdown(signal)}\n"
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
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
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
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
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
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
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

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知
async def notify_highest_analysis_score():
    """
    分析の結果、取引閾値に満たない最高スコア銘柄を1時間ごとに通知する。
    """
    global LAST_ANALYSIS_SIGNALS, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ANALYSIS_ONLY_INTERVAL, SIGNAL_THRESHOLD, GLOBAL_MACRO_CONTEXT, BASE_SCORE
    
    current_time = time.time()
    
    # 実行間隔のチェック
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return

    # 分析結果が格納されているかチェック
    if not LAST_ANALYSIS_SIGNALS:
        logging.info("ℹ️ 分析結果がないため、最高スコア通知をスキップします。")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time 
        return

    # スコアの降順でソートし、最高スコアのシグナルを取得
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)
    best_signal = sorted_signals[0]
    
    best_score = best_signal.get('score', 0.0)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 最高スコアが取引閾値未満で、かつ最低限の分析スコア(例: BASE_SCORE+0.1)を超えている場合
    if best_score < current_threshold and best_score >= (BASE_SCORE + 0.1): 
        
        symbol = best_signal.get('symbol', 'N/A')
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'long')

        # 通知メッセージの整形
        message = (
            f"📈 **分析結果 (最高スコア)** - 定時レポート\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **確認日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
            f"  - **最高スコア銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **最高スコア**: <code>{best_score * 100:.2f} / 100</code> ({side.capitalize()})\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
            f"  - **備考**: 取引閾値 (<code>{current_threshold * 100:.2f}</code>点) に満たなかったため取引はスキップされました。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {BOT_VERSION}</i>"
        )
        
        logging.info(f"ℹ️ 定時報告: 取引閾値未満の最高スコアを通知します: {symbol} ({side}) Score: {best_score:.2f}")
        await send_telegram_notification(message)
    else:
        # 最高スコアが閾値以上、または分析スコアが低すぎる場合は通知スキップ
        if best_score >= current_threshold:
            logging.info("ℹ️ 最高スコアが取引閾値以上のため、定時通知をスキップします (取引シグナルとして通知済みと想定)。")
        else:
             logging.info("ℹ️ 最高スコアが低すぎるため、定時通知をスキップします。")

    # 通知/ログ処理が完了したら時間を更新
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

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
                        if asset.get('asset') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                # どちらか大きい方を使用 (ccxtの計算が間違っている場合に備えて)
                if total_usdt_balance_fallback > total_usdt_balance:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning(f"⚠️ MEXCのフォールバックロジックでUSDT総資産を更新: {format_usdt(total_usdt_balance)}")


        # 総資産をグローバル変数に保存 (リスク管理に使用)
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)}")

        # オープンポジション情報の取得
        # MEXCはfetch_positionsを使用。Binance/Bybitはfetch_positions_riskを使用
        positions: List[Dict] = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions = await EXCHANGE_CLIENT.fetch_positions()

        open_positions = []
        for p in positions:
            # ポジションサイズが0より大きい、かつUSDT建て先物シンボルのみを抽出
            if (p.get('contracts', 0) != 0 or p.get('info', {}).get('vol', 0) != 0) and \
               p.get('symbol', '').endswith('/USDT') and \
               p.get('type') in ['swap', 'future']:
                   
                # 必要な情報を抽出/整形
                entry_price_raw = p.get('entryPrice', 0.0)
                contracts_raw = p.get('contracts', 0.0)
                
                # contractsが取れない場合、MEXC特有の 'vol' フィールドを試す
                if contracts_raw == 0 and EXCHANGE_CLIENT.id == 'mexc':
                    contracts_raw = float(p.get('info', {}).get('vol', 0.0))
                    
                # 名目価値 (Notional Value) を計算
                notional_value = abs(contracts_raw * entry_price_raw)
                
                # side (long/short) の判定
                side = 'long' if contracts_raw > 0 else 'short'
                
                # 最終的なポジション情報 (ボットが管理すべき最低限の情報)
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'contracts': contracts_raw,
                    'entry_price': entry_price_raw,
                    'filled_usdt': notional_value, # 名目価値
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0, # ボット内で動的に設定/管理
                    'take_profit': 0.0, # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE),
                    'raw_info': p, # デバッグ用に生の情報を保持
                })
                
        # グローバル変数 OPEN_POSITIONS を更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        
        logging.info(f"✅ 口座ステータス取得完了。オープンポジション数: {len(OPEN_POSITIONS)}")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False,
        }

    except ccxt.DDoSProtection as e:
        logging.error(f"❌ 口座ステータス取得失敗 - DDoS保護: レートリミットを超えました。{e}")
        return {'total_usdt_balance': 0.0, 'open_positions': OPEN_POSITIONS, 'error': True}
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - ネットワークエラー: {e}")
        return {'total_usdt_balance': 0.0, 'open_positions': OPEN_POSITIONS, 'error': True}
    except Exception as e:
        logging.critical(f"❌ 致命的な口座ステータス取得エラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': OPEN_POSITIONS, 'error': True}


async def fetch_top_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, DEFAULT_SYMBOLS
    
    if SKIP_MARKET_UPDATE or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning("⚠️ 市場更新をスキップ (設定/クライアント未準備)。デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS

    try:
        # 出来高 (Volume) でソートできるティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 💡 致命的エラー修正: fetch_tickersがNoneを返した場合の対策
        if not tickers:
            logging.error("❌ fetch_tickersが空の結果を返しました。デフォルトシンボルを使用します。")
            return DEFAULT_SYMBOLS
            
        # USDT建ての先物/スワップ市場で、出来高 (quoteVolume) のあるものをフィルタリング
        future_usdt_tickers = {
            symbol: ticker 
            for symbol, ticker in tickers.items()
            # quote/settleがUSDTで、かつ先物/スワップ市場であることを確認
            if symbol.endswith('/USDT') and 
               (EXCHANGE_CLIENT.markets.get(symbol, {}).get('type') in ['future', 'swap']) and
               ticker.get('quoteVolume') is not None
        }

        # quoteVolume (USDTの出来高) で降順ソート
        sorted_tickers = sorted(
            future_usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )

        # トップNのシンボルを取得
        top_symbols = [symbol for symbol, _ in sorted_tickers[:limit]]
        
        # トップシンボルリストに、デフォルトシンボルの中から取引中のポジションのシンボルを追加 (ポジションを優先的に監視するため)
        active_position_symbols = [p['symbol'] for p in OPEN_POSITIONS]
        
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS + active_position_symbols))
        
        # 💡 更新: 最新の監視リストをグローバル変数に格納
        CURRENT_MONITOR_SYMBOLS = final_symbols
        logging.info(f"✅ 出来高TOP{limit}とデフォルトリストを統合し、合計 {len(final_symbols)} 銘柄を監視します。")
        return final_symbols

    except ccxt.DDoSProtection as e:
        logging.error(f"❌ fetch_tickers失敗 - DDoS保護: レートリミット超過。デフォルトシンボルを使用します。{e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ fetch_tickers失敗 - 取引所エラー: {e}")
    except Exception as e:
        logging.error(f"❌ fetch_tickers失敗 - 予期せぬエラー: {e}", exc_info=True)

    # エラー時はデフォルトシンボルにフォールバック
    return DEFAULT_SYMBOLS


async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # OHLCVデータを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol, 
            timeframe=timeframe, 
            limit=limit,
            params={'price': 'index'} if EXCHANGE_CLIENT.id == 'mexc' else {} # MEXC Index/Mark Price
        )
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが空でした。")
            return None
            
        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # データが要求された制限を満たしているかチェック
        if len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のデータ数が不足しています: {len(df)}/{limit}")
            return None
            
        return df

    except ccxt.RateLimitExceeded:
        logging.warning(f"⚠️ {symbol} ({timeframe}) でレートリミット超過。スキップします。")
    except ccxt.ExchangeError as e:
        logging.warning(f"⚠️ {symbol} のOHLCV取得中に取引所エラー: {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} のOHLCV取得中に予期せぬエラー: {e}", exc_info=True)
        
    return None

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) を取得し、スコア計算用のプロキシを返す"""
    
    global FGI_API_URL, FGI_PROXY_BONUS_MAX, FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD

    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get('data') and len(data['data']) > 0:
            fgi_value = int(data['data'][0]['value'])
            fgi_classification = data['data'][0]['value_classification']
            
            # FGI (0-100) を -1.0から1.0の範囲に正規化し、マクロ影響プロキシとする
            # 50(Neutral) = 0.0
            # 100(Extreme Greed) = 1.0 (最大ボーナス FGI_PROXY_BONUS_MAX)
            # 0(Extreme Fear) = -1.0 (最大ペナルティ -FGI_PROXY_BONUS_MAX)
            normalized_fgi = (fgi_value - 50) / 50.0
            
            # 実際のスコアへの影響度
            fgi_proxy_bonus = normalized_fgi * FGI_PROXY_BONUS_MAX
            
            logging.info(f"✅ FGI取得: {fgi_value} ({fgi_classification}) -> Proxy Bonus: {fgi_proxy_bonus:+.4f}")

            return {
                'fgi_proxy': fgi_proxy_bonus,
                'fgi_raw_value': fgi_classification,
            }

    except Exception as e:
        logging.warning(f"⚠️ FGIデータ取得失敗: {e}。デフォルト値 (中立) を使用します。")
        
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A'}


# ====================================================================================
# STRATEGY & ANALYSIS
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    指定されたデータフレームにテクニカル指標を追加する。
    """
    # 💡 ボリンジャーバンド (BB)
    df.ta.bbands(length=20, append=True, std=2)
    
    # 💡 ATR (Dynamic SL/TP用)
    df.ta.atr(length=ATR_LENGTH, append=True)

    # 💡 RSI
    df.ta.rsi(length=14, append=True)
    
    # 💡 MACD
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    
    # 💡 SMA (長期トレンド判断用)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    
    # 💡 OBV
    df.ta.obv(append=True)

    # 💡 ADX (トレンド強度)
    df.ta.adx(length=14, append=True)
    
    return df


def generate_signal_score(
    df: pd.DataFrame, 
    symbol: str, 
    timeframe: str, 
    macro_context: Dict,
    liquidity_rank: int,
    total_symbols: int
) -> List[Dict]:
    
    signals = []
    
    # データが空、または計算に必要な行数がない場合はスキップ
    if df.empty or len(df) < max(LONG_TERM_SMA_LENGTH, ATR_LENGTH, 20, 30):
        return signals

    # 最新の足 (現在の未確定の足) ではなく、確定した最新の足を使用するため-2
    # ただし、OHLCVが現在足を含むかどうかは取引所によるため、
    # 厳密には-1 (最新確定足) を使用するのが安全。
    try:
        latest = df.iloc[-1]
        prev = df.iloc[-2]
    except IndexError:
        return signals

    current_price = latest['close']
    
    # スコアブレークダウン用の辞書
    tech_data = {
        'base_score': BASE_SCORE,
        'long_term_reversal_penalty_value': 0.0,
        'macd_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': macro_context.get('fgi_proxy', 0.0), # FGIは直接加算
        'volatility_penalty_value': 0.0,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
    }
    
    # ----------------------------------------------------
    # 1. 流動性ボーナス (Liquidity Bonus) 
    # ----------------------------------------------------
    # TOP_SYMBOL_LIMIT にランクインしている銘柄にボーナス
    # 1位で最大 (LIQUIDITY_BONUS_MAX)、最下位で0に収束
    if liquidity_rank <= TOP_SYMBOL_LIMIT and total_symbols > 0:
        # ランクに応じて線形的にボーナスを減衰
        liq_bonus = LIQUIDITY_BONUS_MAX * (1 - (liquidity_rank - 1) / TOP_SYMBOL_LIMIT)
        liq_bonus = max(0.0, min(liq_bonus, LIQUIDITY_BONUS_MAX)) # 0から最大値の間に制限
        tech_data['liquidity_bonus_value'] = liq_bonus
        
    # ----------------------------------------------------
    # 2. ボラティリティペナルティ (Volatility Penalty) 
    # ----------------------------------------------------
    # ボリンジャーバンドのバンド幅が広すぎる場合、ペナルティ
    bb_width = latest[f'BBM_20_2.0'] * (latest[f'BBP_20_2.0'] - latest[f'BBL_20_2.0'])
    bb_width_ratio = bb_width / current_price if current_price > 0 else 0.0
    
    volatility_penalty = 0.0
    if bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
        # 閾値を超えた分だけペナルティを課す (最大ペナルティはMACD_CROSS_PENALTYと同じにする)
        volatility_penalty = -MACD_CROSS_PENALTY * (bb_width_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) / (VOLATILITY_BB_PENALTY_THRESHOLD * 2) # 例: 閾値の3倍で最大ペナルティ
        volatility_penalty = max(volatility_penalty, -MACD_CROSS_PENALTY)
        tech_data['volatility_penalty_value'] = volatility_penalty


    # ----------------------------------------------------
    # 3. ロング/ショート シグナルをそれぞれ評価
    # ----------------------------------------------------

    # --- ロング評価 ---
    long_score_raw = BASE_SCORE + STRUCTURAL_PIVOT_BONUS + tech_data['liquidity_bonus_value'] + tech_data['sentiment_fgi_proxy_bonus'] + tech_data['volatility_penalty_value']
    long_bonus = 0.0
    
    # a. 長期トレンドとの一致 (SMA 200)
    # 価格がSMA200の上にある (順張りボーナス) / 下にある (逆張りペナルティ)
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    if latest[sma_col] > 0:
        if current_price > latest[sma_col]:
            long_bonus += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
        else:
            long_bonus -= LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
            tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY
    
    # b. MACDの方向一致 (ゴールデンクロス/デッドクロス後のモメンタム)
    macd_hist = latest['MACDh_12_26_9']
    prev_macd_hist = prev['MACDh_12_26_9']
    
    macd_val = 0.0
    if macd_hist > 0 and prev_macd_hist > 0:
        macd_val = MACD_CROSS_PENALTY # MACDが上向きでモメンタム継続 (Longボーナス)
    elif macd_hist < 0 and prev_macd_hist < 0:
        macd_val = -MACD_CROSS_PENALTY # MACDが下向きでモメンタム継続 (Longペナルティ)
    
    long_bonus += macd_val
    tech_data['macd_penalty_value'] = macd_val

    # c. RSIモメンタム (40-60の適正水準、または40からの反発)
    rsi_col = 'RSI_14'
    rsi_val = 0.0
    if RSI_MOMENTUM_LOW <= latest[rsi_col] <= 60:
        # RSIが適正水準 (レンジ) にある (ロング・ショート共通のモメンタムボーナス)
        rsi_val = MACD_CROSS_PENALTY * 0.5 
    elif latest[rsi_col] < RSI_MOMENTUM_LOW and latest[rsi_col] > prev[rsi_col]:
        # RSIが売られすぎ水準から反発傾向 (ロングボーナス)
        rsi_val = RSI_DIVERGENCE_BONUS 
    
    long_bonus += rsi_val
    tech_data['rsi_momentum_bonus_value'] = rsi_val
    
    # d. OBV (出来高) の確証
    obv_val = 0.0
    if latest['OBV'] > prev['OBV'] * 1.0001: # OBVが前日よりわずかでも上昇
        obv_val = OBV_MOMENTUM_BONUS
    
    long_bonus += obv_val
    tech_data['obv_momentum_bonus_value'] = obv_val

    long_score_final = long_score_raw + long_bonus

    
    # --- ショート評価 ---
    short_score_raw = BASE_SCORE + STRUCTURAL_PIVOT_BONUS + tech_data['liquidity_bonus_value'] + tech_data['sentiment_fgi_proxy_bonus'] + tech_data['volatility_penalty_value']
    short_bonus = 0.0
    
    # a. 長期トレンドとの一致 (SMA 200)
    if latest[sma_col] > 0:
        if current_price < latest[sma_col]:
            short_bonus += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        else:
            short_bonus -= LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    
    # b. MACDの方向一致 (ゴールデンクロス/デッドクロス後のモメンタム)
    macd_val_short = 0.0
    if macd_hist < 0 and prev_macd_hist < 0:
        macd_val_short = MACD_CROSS_PENALTY # MACDが下向きでモメンタム継続 (Shortボーナス)
    elif macd_hist > 0 and prev_macd_hist > 0:
        macd_val_short = -MACD_CROSS_PENALTY # MACDが上向きでモメンタム継続 (Shortペナルティ)
    
    short_bonus += macd_val_short

    # c. RSIモメンタム (40-60の適正水準、または60からの反落)
    rsi_val_short = 0.0
    if RSI_MOMENTUM_LOW <= latest[rsi_col] <= 60:
        rsi_val_short = MACD_CROSS_PENALTY * 0.5 
    elif latest[rsi_col] > 60 and latest[rsi_col] < prev[rsi_col]:
        # RSIが買われすぎ水準から反落傾向 (ショートボーナス)
        rsi_val_short = RSI_DIVERGENCE_BONUS 
        
    short_bonus += rsi_val_short
    
    # d. OBV (出来高) の確証
    obv_val_short = 0.0
    if latest['OBV'] < prev['OBV'] * 0.9999: # OBVが前日よりわずかでも減少
        obv_val_short = OBV_MOMENTUM_BONUS
        
    short_bonus += obv_val_short
    
    short_score_final = short_score_raw + short_bonus
    
    
    # ----------------------------------------------------
    # 4. ATRに基づくSL/TPの計算とシグナル生成
    # ----------------------------------------------------
    
    latest_atr = latest[f'ATR_{ATR_LENGTH}']
    
    if latest_atr is not None and not np.isnan(latest_atr):
        
        # ATRに基づいたリスク幅 (SLまでの距離)
        atr_risk_distance = latest_atr * ATR_MULTIPLIER_SL
        
        # 最小リスク幅の強制適用 (価格のMIN_RISK_PERCENT)
        min_risk_distance = current_price * MIN_RISK_PERCENT 
        final_risk_distance = max(atr_risk_distance, min_risk_distance)
        
        # リワード幅 (TPまでの距離) はRRRを元に計算
        reward_distance = final_risk_distance * RR_RATIO_TARGET

        # --- Long SL/TP ---
        long_sl = current_price - final_risk_distance
        long_tp = current_price + reward_distance
        long_liquidation_price = calculate_liquidation_price(
            current_price, LEVERAGE, 'long', MIN_MAINTENANCE_MARGIN_RATE
        )
        
        # --- Short SL/TP ---
        short_sl = current_price + final_risk_distance
        short_tp = current_price - reward_distance
        short_liquidation_price = calculate_liquidation_price(
            current_price, LEVERAGE, 'short', MIN_MAINTENANCE_MARGIN_RATE
        )
        
        # Longシグナルを追加
        if long_score_final > BASE_SCORE:
            signals.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'side': 'long',
                'score': long_score_final,
                'entry_price': current_price,
                'stop_loss': long_sl,
                'take_profit': long_tp,
                'liquidation_price': long_liquidation_price,
                'risk_distance': final_risk_distance,
                'reward_distance': reward_distance,
                'rr_ratio': RR_RATIO_TARGET,
                'tech_data': tech_data.copy() # ディープコピー
            })
            
        # Shortシグナルを追加
        if short_score_final > BASE_SCORE:
            # Shortシグナルの場合は、長期トレンドペナルティをShort版に置き換え
            short_tech_data = tech_data.copy()
            if latest[sma_col] > 0:
                short_tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY if current_price < latest[sma_col] else -LONG_TERM_REVERSAL_PENALTY
            short_tech_data['macd_penalty_value'] = macd_val_short
            short_tech_data['rsi_momentum_bonus_value'] = rsi_val_short
            short_tech_data['obv_momentum_bonus_value'] = obv_val_short
            
            signals.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'side': 'short',
                'score': short_score_final,
                'entry_price': current_price,
                'stop_loss': short_sl,
                'take_profit': short_tp,
                'liquidation_price': short_liquidation_price,
                'risk_distance': final_risk_distance,
                'reward_distance': reward_distance,
                'rr_ratio': RR_RATIO_TARGET,
                'tech_data': short_tech_data
            })
            
    return signals


async def run_analysis(symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    全監視銘柄と全タイムフレームで分析を実行し、シグナルを生成する。
    """
    global LAST_ANALYSIS_SIGNALS, REQUIRED_OHLCV_LIMITS, TARGET_TIMEFRAMES
    
    all_signals: List[Dict] = []
    
    logging.info(f"📊 分析開始: 監視銘柄数 {len(symbols)} / タイムフレーム {TARGET_TIMEFRAMES}")
    
    # 出来高ランクを計算 (流動性ボーナス用)
    liquidity_ranks = {symbol: i + 1 for i, symbol in enumerate(symbols)}
    total_symbols = len(symbols)
    
    for symbol in symbols:
        # 既にオープンポジションがある場合は、ポジションの決済ロジックに任せるため、
        # 新規シグナルの生成はスキップする (リスクを限定するため)
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            logging.info(f"ℹ️ {symbol}: ポジション管理中につき、新規シグナル生成はスキップします。")
            continue
            
        for tf in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 1000)
            df = await fetch_ohlcv_data(symbol, tf, limit)
            
            if df is not None and not df.empty:
                df = calculate_technical_indicators(df, tf)
                
                # シグナル生成
                new_signals = generate_signal_score(
                    df, 
                    symbol, 
                    tf, 
                    macro_context,
                    liquidity_ranks.get(symbol, total_symbols), # ランク情報
                    total_symbols
                )
                
                # スコア順にソートして、最も強いシグナルのみを保持
                if new_signals:
                    new_signals.sort(key=lambda x: x['score'], reverse=True)
                    all_signals.extend(new_signals[:1]) # 各タイムフレームで最も強いシグナル1つだけを採用

            # レートリミット対策として短い遅延を入れる
            await asyncio.sleep(0.05) 
            
    # 全タイムフレームで生成されたシグナルをスコア降順でソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # グローバル変数に最新の分析結果を保存
    LAST_ANALYSIS_SIGNALS = all_signals
    
    logging.info(f"✅ 分析完了。検出されたシグナル総数: {len(all_signals)}")
    return all_signals

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

async def execute_trade(signal: Dict) -> Optional[Dict]:
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    if TEST_MODE or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.warning(f"⚠️ TEST_MODE/クライアント未準備のため、{symbol} ({side}) の取引をスキップしました。")
        return {'status': 'skip', 'error_message': 'TEST_MODE is active.', 'filled_usdt': FIXED_NOTIONAL_USDT}
        
    try:
        # 1. ロットサイズの決定 (固定ロット)
        # 現在の口座資産を考慮しない単純な固定名目価値
        notional_size_usdt = FIXED_NOTIONAL_USDT
        
        # 2. 注文数量の計算 (契約数)
        # 数量 = 名目価値 / エントリー価格
        amount_to_trade = notional_size_usdt / entry_price
        
        # 3. 注文
        # CCXTの `create_order` は数量 (amount) を引数に取る
        
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 💡 致命的エラー修正強化: Amount can not be less than zero対策
        if amount_to_trade <= 0:
             raise ValueError(f"計算された注文数量 ({amount_to_trade:.8f}) が無効です。")
             
        # CCXTのシンボルに変換 (例: BTC/USDT -> BTC/USDT)
        ccxt_symbol = symbol 

        # 成行注文 (Market Order) で発注
        order = await EXCHANGE_CLIENT.create_order(
            symbol=ccxt_symbol,
            type='market',
            side=order_side,
            amount=amount_to_trade,
            params={
                'leverage': LEVERAGE, 
                'positionSide': side.capitalize(), # Long/Short/Both (取引所依存)
                'clientOrderId': f"apex_{uuid.uuid4()}",
            }
        )

        logging.info(f"✅ {symbol} ({side}) の成行注文を執行しました。数量: {amount_to_trade:.4f}")
        
        # 実際は約定情報を待つ必要があるが、ここでは簡略化のため注文オブジェクトから取得
        filled_amount = float(order.get('filled', amount_to_trade))
        filled_price = float(order.get('price', entry_price)) # 約定価格、なければシグナル価格
        
        # 注文結果を返す
        return {
            'status': 'ok',
            'symbol': symbol,
            'side': side,
            'filled_amount': filled_amount,
            'filled_price': filled_price,
            'filled_usdt': filled_amount * filled_price, # 実際の約定名目価値
            'order_id': order.get('id'),
            'timestamp': order.get('timestamp')
        }

    except ValueError as e:
        logging.critical(f"❌ 注文失敗 - ロット計算エラー: {e}")
        return {'status': 'error', 'error_message': str(e)}
    except ccxt.ExchangeError as e:
        logging.critical(f"❌ 注文失敗 - 取引所エラー: {e}")
        return {'status': 'error', 'error_message': f"Exchange Error: {e}"}
    except ccxt.NetworkError as e:
        logging.error(f"❌ 注文失敗 - ネットワークエラー: {e}")
        return {'status': 'error', 'error_message': f"Network Error: {e}"}
    except Exception as e:
        logging.critical(f"❌ 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f"Unexpected Error: {e}"}


async def process_entry_signal(signal: Dict, current_threshold: float) -> Optional[Dict]:
    """
    シグナルを評価し、取引を実行する。
    """
    symbol = signal['symbol']
    side = signal.get('side', 'long')
    score = signal['score']
    
    # 最終的な取引閾値チェック
    if score < current_threshold:
        log_signal(signal, "シグナル不採用 (閾値未満)")
        return None

    # クールダウンチェック (同一銘柄での直近の取引を避ける)
    current_time = time.time()
    last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
    if current_time - last_signal_time < TRADE_SIGNAL_COOLDOWN:
        cooldown_hours = TRADE_SIGNAL_COOLDOWN / 3600
        logging.warning(f"⚠️ {symbol}: クールダウン期間中 ({cooldown_hours:.1f}時間)。取引をスキップします。")
        log_signal(signal, "シグナル不採用 (クールダウン)")
        return None
        
    # オープンポジションの重複チェック
    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
        logging.warning(f"⚠️ {symbol}: 既にオープンポジションがあります。新規エントリーをスキップします。")
        log_signal(signal, "シグナル不採用 (ポジション重複)")
        return None
        
    # 実際に取引を執行
    trade_result = await execute_trade(signal)
    
    # 実行結果をログに記録
    log_signal(signal, "取引シグナル", trade_result)
    
    # Telegram通知を送信
    await send_telegram_notification(
        format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
    )

    if trade_result and trade_result.get('status') == 'ok':
        # 成功した場合、最終的なポジション情報を更新するために、グローバルポジションリストを再構築/更新する必要がある
        # しかし、ここではシンプルにグローバル変数 `OPEN_POSITIONS` を再取得/更新することで対応する
        await fetch_account_status()
        
        # 最終的なポジション情報 (ST/TP設定のため、この後のロジックで必要)
        new_position_data = next((p for p in OPEN_POSITIONS if p['symbol'] == symbol), None)
        
        if new_position_data:
            # 新しいポジション情報にSL/TPをコピー
            new_position_data['stop_loss'] = signal['stop_loss']
            new_position_data['take_profit'] = signal['take_profit']
            
            # ST/TP注文の発注 (成功したポジションに対してのみ)
            await set_stop_loss_and_take_profit(new_position_data)
        
        # クールダウン時間を更新
        LAST_SIGNAL_TIME[symbol] = current_time
        return trade_result

    return trade_result


async def set_stop_loss_and_take_profit(position: Dict) -> bool:
    """
    ポジションに対して、SL/TPのOCO注文 (または同等のもの) を設定する。
    MEXC/Bybit/Binanceでは、ポジションにトリガー価格を設定する。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    contracts = abs(position['contracts']) # 契約数は正の値
    
    if TEST_MODE or not EXCHANGE_CLIENT or not IS_CLIENT_READY or contracts == 0:
        logging.warning(f"⚠️ TEST_MODE/クライアント未準備/契約数0のため、{symbol} のSL/TP設定をスキップしました。")
        return False
        
    try:
        # ポジションのクローズ注文なので、サイドは逆になる
        close_side = 'sell' if side == 'long' else 'buy'
        
        # 注文タイプ: Stop/Limit/TakeProfit/Marketなど (取引所によって異なる)
        # ほとんどの取引所では、ポジションに対する Stop Market/Limit が利用可能
        
        # 1. 既存のSL/TP注文をキャンセル (既に設定されていた場合)
        # CCXTには一括キャンセル/ポジションIDによるキャンセルがないため、ここでは割愛
        
        # 2. SLとTPの注文を個別に、またはOCOとして発注
        
        # MEXCの場合: set_position_mode, set_margin_mode, set_leverage, ... は既に実行済みと仮定
        
        # CCXTはStop LossとTake Profitをまとめて設定するメソッドを提供している (set_position_risk / edit_position)
        # ただし、MEXCはこれをサポートしていない可能性があるため、`create_order`でトリガー注文を試みる
        
        # 🚨 CCXTの 'take_profit' / 'stop_loss' パラメータは、取引所によって挙動が異なるか、
        # あるいはポジション管理画面での設定に対応していない場合がある。
        # ここでは、一般的な `create_order` で「トリガー注文」を発行するロジックを試みる。

        # --- Stop Loss 注文 (逆指値 - Market) ---
        sl_order_params = {
             'stopLossPrice': stop_loss, # トリガー価格
             'trigger': 'mark' if EXCHANGE_CLIENT.id == 'bybit' or EXCHANGE_CLIENT.id == 'binance' else 'market', # トリガータイプ (MEXCはprice/market/index)
             'reduceOnly': True, # ポジション削減のみを目的とする
        }
        
        # MEXCはトリガー注文に price(Market), triggerType を要求
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCのストップ注文は 'market' タイプでトリガー価格を指定
             sl_order_params = {
                 'stopLossPrice': stop_loss, # トリガー価格
                 'triggerPrice': stop_loss, # トリガー価格 (別名の場合)
                 'triggerType': 1, # 1: Market
                 'reduceOnly': True,
             }

        sl_order = await EXCHANGE_CLIENT.create_order(
             symbol=symbol,
             type='market', # 成行注文で決済
             side=close_side,
             amount=contracts,
             price=stop_loss, # Stop Loss のトリガー価格を price に渡す (取引所依存)
             params=sl_order_params
        )
        
        logging.info(f"✅ {symbol} のSL注文 (トリガー: {format_price(stop_loss)}) を設定しました。ID: {sl_order.get('id')}")

        
        # --- Take Profit 注文 (利食い - Limit) ---
        tp_order_params = {
             'takeProfitPrice': take_profit, # トリガー価格
             'trigger': 'mark' if EXCHANGE_CLIENT.id == 'bybit' or EXCHANGE_CLIENT.id == 'binance' else 'market',
             'reduceOnly': True,
        }
        
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCのテイクプロフィット注文
             tp_order_params = {
                 'takeProfitPrice': take_profit, # トリガー価格
                 'triggerPrice': take_profit, # トリガー価格 (別名の場合)
                 'triggerType': 1, # 1: Market (または Limit の場合は 2)
                 'reduceOnly': True,
             }

        tp_order = await EXCHANGE_CLIENT.create_order(
             symbol=symbol,
             type='market', # 成行注文で決済
             side=close_side,
             amount=contracts,
             price=take_profit, # Take Profit のトリガー価格を price に渡す (取引所依存)
             params=tp_order_params
        )
        
        logging.info(f"✅ {symbol} のTP注文 (トリガー: {format_price(take_profit)}) を設定しました。ID: {tp_order.get('id')}")

        return True

    except ccxt.ExchangeError as e:
        logging.critical(f"❌ SL/TP注文設定失敗 - 取引所エラー: {e}")
        await send_telegram_notification(f"🚨 **SL/TP設定エラー** ({symbol})\n取引所エラーにより、ポジションのSL/TP設定に失敗しました: <code>{e}</code>")
        return False
    except Exception as e:
        logging.critical(f"❌ SL/TP注文設定失敗 - 予期せぬエラー: {e}", exc_info=True)
        return False


async def position_monitor_and_update_sltp() -> None:
    """
    オープンポジションを監視し、SL/TPに達しているか、または清算されているかを確認し、必要に応じて決済処理を行う。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"👀 ポジション監視中: {len(OPEN_POSITIONS)} 銘柄")
    
    current_prices: Dict[str, float] = {}
    
    # 監視中の全シンボルの最新価格を一括取得
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers([p['symbol'] for p in OPEN_POSITIONS])
        for symbol, ticker in tickers.items():
            if ticker and ticker.get('last') is not None:
                current_prices[symbol] = float(ticker['last'])
    except Exception as e:
        logging.error(f"❌ 価格一括取得失敗: {e}。監視をスキップします。")
        return

    positions_to_close: List[Tuple[Dict, str]] = [] # (position, exit_type)

    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        contracts = position['contracts']
        entry_price = position['entry_price']
        stop_loss = position['stop_loss']
        take_profit = position['take_profit']
        
        current_price = current_prices.get(symbol)
        
        if not current_price:
            logging.warning(f"⚠️ {symbol} の最新価格が取得できませんでした。監視をスキップします。")
            continue
            
        exit_type = None
        
        # SL/TPのチェック (Market Priceベースで判定)
        if side == 'long':
            # ロング: SL (価格下落)、TP (価格上昇)
            if current_price <= stop_loss and stop_loss > 0:
                exit_type = "SL (ストップロス)"
            elif current_price >= take_profit and take_profit > 0:
                exit_type = "TP (テイクプロフィット)"
        elif side == 'short':
            # ショート: SL (価格上昇)、TP (価格下落)
            if current_price >= stop_loss and stop_loss > 0:
                exit_type = "SL (ストップロス)"
            elif current_price <= take_profit and take_profit > 0:
                exit_type = "TP (テイクプロフィット)"
                
        # 清算価格のチェック (念のため)
        if exit_type is None and position.get('liquidation_price', 0) > 0:
            liq_price = position['liquidation_price']
            # 清算価格に近すぎる場合 (取引所のマージンコールを信頼する)
            if side == 'long' and current_price <= liq_price * 1.05:
                # ロングで清算価格に近づいている場合
                logging.warning(f"⚠️ {symbol} ロングが清算価格 {format_price(liq_price)} に近づいています (現在: {format_price(current_price)})。")
            elif side == 'short' and current_price >= liq_price * 0.95:
                 # ショートで清算価格に近づいている場合
                logging.warning(f"⚠️ {symbol} ショートが清算価格 {format_price(liq_price)} に近づいています (現在: {format_price(current_price)})。")
                
            # ここでは、清算 (Liquidation) 自体は取引所任せとし、SL/TPでの決済に専念する
        
        
        if exit_type:
            positions_to_close.append((position, exit_type))
            logging.info(f"🔥 決済トリガー: {symbol} ({side}) - {exit_type} at {format_price(current_price)}")
            
            
    # 決済が必要なポジションを処理
    for position, exit_type in positions_to_close:
        
        symbol = position['symbol']
        side = position['side']
        contracts_abs = abs(position['contracts'])
        
        # 1. SL/TP注文をキャンセル (既に発注済みの注文をキャンセルし、重複決済を避ける)
        try:
            # 💡 致命的エラー修正強化: MEXCでは未対応のメソッドを避ける
            if EXCHANGE_CLIENT.has['cancelAllOrders']:
                 await EXCHANGE_CLIENT.cancel_all_orders(symbol)
                 logging.info(f"✅ {symbol} の未執行注文を全てキャンセルしました。")
        except Exception as e:
            logging.warning(f"⚠️ {symbol} の注文キャンセルに失敗: {e}")
            
        # 2. ポジションを成行注文でクローズ
        close_side = 'sell' if side == 'long' else 'buy'
        
        trade_result: Optional[Dict] = None
        
        if contracts_abs > 0 and not TEST_MODE:
            try:
                # ポジションクローズのための成行注文
                close_order = await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type='market',
                    side=close_side,
                    amount=contracts_abs,
                    params={
                        'reduceOnly': True, # ポジション削減のみ
                        'clientOrderId': f"apex_close_{uuid.uuid4()}",
                    }
                )
                
                # 約定情報を取得 (ここでは簡略化のため、最新価格を決済価格とする)
                exit_price = current_prices.get(symbol, position['entry_price'])
                filled_amount = float(close_order.get('filled', contracts_abs))
                
                # PNL計算
                entry_price = position['entry_price']
                
                if side == 'long':
                    pnl_usdt = filled_amount * (exit_price - entry_price)
                else: # short
                    pnl_usdt = filled_amount * (entry_price - exit_price)
                    
                # 証拠金に対するPNL率を概算 (レバレッジ考慮なしの名目ロットベースで計算)
                notional_value = position['filled_usdt']
                pnl_rate = pnl_usdt / notional_value if notional_value > 0 else 0.0
                
                trade_result = {
                    'status': 'ok',
                    'symbol': symbol,
                    'side': side,
                    'filled_amount': filled_amount,
                    'exit_price': exit_price,
                    'entry_price': entry_price,
                    'pnl_usdt': pnl_usdt,
                    'pnl_rate': pnl_rate,
                    'exit_type': exit_type,
                    'order_id': close_order.get('id'),
                }
                
                logging.info(f"✅ {symbol} ({side}) ポジションを {exit_type} で決済しました。PNL: {pnl_usdt:+.2f} USDT")

            except ccxt.ExchangeError as e:
                logging.critical(f"❌ 決済注文失敗 - 取引所エラー: {e}")
                trade_result = {'status': 'error', 'error_message': f"Close Error: {e}", 'exit_type': exit_type}
            except Exception as e:
                logging.critical(f"❌ 決済注文失敗 - 予期せぬエラー: {e}", exc_info=True)
                trade_result = {'status': 'error', 'error_message': f"Unexpected Close Error: {e}", 'exit_type': exit_type}
        
        else: # TEST_MODEの場合
             exit_price = current_prices.get(symbol, position['entry_price'])
             entry_price = position['entry_price']
             
             if side == 'long':
                pnl_usdt = contracts_abs * (exit_price - entry_price)
             else: # short
                pnl_usdt = contracts_abs * (entry_price - exit_price)
                
             notional_value = position['filled_usdt']
             pnl_rate = pnl_usdt / notional_value if notional_value > 0 else 0.0
             
             trade_result = {
                'status': 'skip',
                'symbol': symbol,
                'side': side,
                'filled_amount': contracts_abs,
                'exit_price': exit_price,
                'entry_price': entry_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': exit_type,
                'order_id': 'TEST_MODE_SKIP',
            }
             logging.info(f"⚠️ TEST_MODEのため、{symbol} ({side}) ポジション決済をスキップしました (Exit Type: {exit_type})。")


        # 3. ログと通知を記録
        # 決済時のメッセージ整形には、元のポジション情報とtrade_resultをマージして渡す
        position_with_result = position.copy()
        position_with_result.update(trade_result)
        
        log_signal(position_with_result, "ポジション決済", trade_result)
        
        # Telegram通知
        await send_telegram_notification(
            format_telegram_message(position_with_result, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
        )
        
    # 処理後、グローバルポジションリストを更新
    if positions_to_close or any(p['contracts'] == 0 for p in OPEN_POSITIONS):
        # 決済処理によりポジションリストが変更された可能性が高いため、再取得
        await fetch_account_status()


# ====================================================================================
# MAIN SCHEDULER LOGIC
# ====================================================================================

async def main_bot_scheduler():
    """
    メインのBOT実行スケジューラ。
    定期的に市場データを取得、分析、シグナル処理を行う。
    """
    global IS_CLIENT_READY, LAST_SUCCESS_TIME, LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED

    logging.info("⚙️ メインBOTスケジューラを開始します。")

    # 1. クライアントの初期化
    if not await initialize_exchange_client():
        logging.critical("❌ CCXTクライアントの初期化に失敗しました。BOTを停止します。")
        await send_telegram_notification("🚨 **BOT停止通知**\n<code>CCXTクライアントの初期化 (APIキー/認証) に失敗したため、BOTを停止します。ログを確認してください。</code>")
        return

    # 2. 初期口座ステータスの取得
    account_status = await fetch_account_status()
    
    # 3. FGI (マクロ環境) の初期取得
    fgi_data = await fetch_fgi_data()
    GLOBAL_MACRO_CONTEXT.update(fgi_data)

    # 4. BOT起動完了通知 (最初の1回のみ)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    await send_telegram_notification(
        format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    )

    while True:
        try:
            start_time = time.time()
            
            # --- 1. 定期的な口座ステータスと市場の更新 ---
            # ポジション監視の精度を上げるため、ループの最初に実行
            await fetch_account_status()
            
            # 出来高TOP銘柄を定期的に更新
            if time.time() - LAST_SUCCESS_TIME > LOOP_INTERVAL:
                logging.info("--- 🔄 メインループ実行中 ---")
                
                # 市場環境の更新
                fgi_data = await fetch_fgi_data()
                GLOBAL_MACRO_CONTEXT.update(fgi_data)
                
                # 監視銘柄リストの更新 (レートリミットを考慮し、頻度を抑える)
                monitoring_symbols = await fetch_top_symbols()
                
                # 現在の動的取引閾値を決定
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

                # --- 2. 分析とシグナル生成 ---
                signals = await run_analysis(monitoring_symbols, GLOBAL_MACRO_CONTEXT)
                
                # --- 3. シグナル処理 (スコア順に処理) ---
                processed_count = 0
                for signal in signals:
                    # 既にTOP_SIGNAL_COUNTに達しているか、またはポジションがある場合はスキップ
                    if processed_count >= TOP_SIGNAL_COUNT:
                        break
                        
                    trade_result = await process_entry_signal(signal, current_threshold)
                    if trade_result and trade_result.get('status') == 'ok':
                        processed_count += 1
                        
                if processed_count == 0 and signals:
                     # 実行はなかったが、シグナルは存在した場合、最高スコアをログに記録
                     highest_signal = signals[0]
                     log_signal(highest_signal, "最高スコア (取引スキップ)")
                     
                
                # --- 4. 定時通知 (閾値未満の最高スコア) ---
                await notify_highest_analysis_score()


                # --- 5. WebShareへのデータ送信 (定期実行) ---
                if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                    webshare_data = {
                        'timestamp': datetime.now(JST).isoformat(),
                        'bot_version': BOT_VERSION,
                        'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                        'open_positions_count': len(OPEN_POSITIONS),
                        'macro_context': GLOBAL_MACRO_CONTEXT,
                        'top_signal': LAST_ANALYSIS_SIGNALS[0] if LAST_ANALYSIS_SIGNALS else None,
                        'current_threshold': current_threshold,
                    }
                    await send_webshare_update(webshare_data)
                    LAST_WEBSHARE_UPLOAD_TIME = time.time()


                end_time = time.time()
                elapsed = end_time - start_time
                logging.info(f"✅ メインループ完了。所要時間: {elapsed:.2f}秒。")
                LAST_SUCCESS_TIME = end_time
                IS_FIRST_MAIN_LOOP_COMPLETED = True


            # --- 6. 次のループまで待機 ---
            sleep_duration = LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME)
            if sleep_duration > 0:
                await asyncio.sleep(sleep_duration)
            else:
                await asyncio.sleep(0.1) # 無限ループ防止用の最低遅延

        except ccxt.ExchangeNotAvailable as e:
            logging.critical(f"❌ 取引所接続エラー: サーバーが利用できません。リトライします。{e}")
            await asyncio.sleep(60) # 1分待機
        except ccxt.NetworkError as e:
            logging.error(f"❌ ネットワークエラー: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            logging.critical(f"❌ 致命的なメインループエラー: {e}", exc_info=True)
            # 致命的エラーの場合はTelegram通知を送信
            await send_telegram_notification(f"🚨 **致命的エラー発生**\n<code>メインBOTループ内で未処理のエラーが発生しました: {e}</code>")
            # 致命的エラー後にクライアントを再初期化し、リソースを解放する
            IS_CLIENT_READY = False
            await initialize_exchange_client()
            await asyncio.sleep(30)


async def position_monitor_scheduler():
    """
    ポジション監視専用スケジューラ。メインループとは独立して動作する。
    """
    logging.info("⚙️ ポジション監視スケジューラを開始します。")
    
    # メインループが起動するまで待機
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        await asyncio.sleep(MONITOR_INTERVAL)
        
    while True:
        try:
            # ポジションを監視し、TP/SLトリガーをチェック (レートリミットに注意)
            await position_monitor_and_update_sltp()
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに監視


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
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        # Telegram通知はループ内で実行されるため、ここでは省略
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )


if __name__ == '__main__':
    # 環境変数をチェックし、APIキーがない場合は起動を停止
    if not API_KEY or not SECRET_KEY:
        logging.critical("❌ 環境変数にAPIキー/シークレットが設定されていません。BOTを起動できません。")
        sys.exit(1)
        
    # Uvicornサーバーの起動
    logging.info(f"🤖 Apex BOT {BOT_VERSION} を起動します...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
