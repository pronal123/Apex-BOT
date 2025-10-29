# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled。")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

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
                    'raw_info': p, # デバッグ用
                })

        # グローバルなOPEN_POSITIONSを更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"✅ オープンポジション ({len(OPEN_POSITIONS)}件) の情報を取得しました。")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_tickers() -> Dict[str, Any]:
    """
    全シンボルの最新ティッカー価格を取得し、出来高TOP銘柄をフィルタリングする。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ティッカー情報取得失敗: CCXTクライアントが準備できていません。")
        return {}
        
    try:
        # fetch_tickersは大量のシンボルを返すため、レートリミットに注意が必要
        tickers = await EXCHANGE_CLIENT.fetch_tickers() 
        
        if not tickers or not isinstance(tickers, dict):
             # 💡 修正: 空または不正なデータの場合は警告ログ
             logging.warning("⚠️ fetch_tickersが空または不正なデータを返しました。")
             return {}
             
        # フィルタリングと整形
        future_tickers = {}
        for symbol, ticker in tickers.items():
            # 必須チェック: USDT建て先物、かつ24時間出来高 (quoteVolume) が存在すること
            if symbol.endswith('/USDT') and 'future' in ticker.get('info', {}).get('symbol', '').lower() and ticker.get('quoteVolume') is not None:
                # CCXT標準シンボル名に整形 (例: BTC/USDT)
                standard_symbol = ticker['symbol'] 
                
                # 出来高 (USDT)
                volume = ticker['quoteVolume'] 
                
                future_tickers[standard_symbol] = {
                    'symbol': standard_symbol,
                    'price': ticker.get('last'),
                    'volume_usdt_24h': volume,
                    'raw_ticker': ticker,
                }

        # 出来高降順でソートし、TOP_SYMBOL_LIMITで制限
        sorted_tickers = sorted(
            future_tickers.values(), 
            key=lambda x: x.get('volume_usdt_24h', 0.0), 
            reverse=True
        )
        
        # TOP_SYMBOL_LIMITで制限し、DEFAULT_SYMBOLSと結合して重複を排除
        top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # 最終的な監視対象リスト
        global CURRENT_MONITOR_SYMBOLS
        current_symbols_set = set(DEFAULT_SYMBOLS)
        current_symbols_set.update(top_symbols)
        CURRENT_MONITOR_SYMBOLS = list(current_symbols_set)

        logging.info(f"✅ ティッカー情報取得完了。監視銘柄を {len(CURRENT_MONITOR_SYMBOLS)} 件に更新しました。")
        
        # ティッカー情報全体を返す
        return future_tickers

    except ccxt.ExchangeError as e:
        logging.error(f"❌ ティッカー情報取得失敗 - 取引所エラー: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ ティッカー情報取得失敗 - ネットワークエラー: {e}")
    except Exception as e:
        # 💡 修正: fetch_tickersのAttributeError対策として汎用的なエラーハンドリングを強化
        logging.error(f"❌ ティッカー情報取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return {}


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """
    指定されたシンボル、時間足、本数のOHLCVデータを取得する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}/{timeframe}): CCXTクライアントが準備できていません。")
        return None
        
    # CCXTシンボル形式に変換 (例: BTC/USDT)
    ccxt_symbol = symbol
    
    try:
        # `ccxt.fetch_ohlcv`はミリ秒のタイムスタンプ、open, high, low, close, volumeのリストのリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            ccxt_symbol, 
            timeframe, 
            limit=limit
        )
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ OHLCVデータが不足しています ({symbol}/{timeframe} - 取得: {len(ohlcv)} < 要求: {limit})")
            return None

        # Pandas DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # タイムスタンプをdatetimeオブジェクトに変換し、インデックスに設定
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        
        # データの欠損チェック (終値がNaNのデータがあれば除去)
        df.dropna(subset=['close'], inplace=True) 
        
        logging.info(f"✅ OHLCVデータ取得完了 ({symbol}/{timeframe} - {len(df)}本)")
        return df

    except ccxt.ExchangeError as e:
        logging.error(f"❌ OHLCV取得失敗 - 取引所エラー: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCV取得失敗 - ネットワークエラー: {e}")
    except Exception as e:
        logging.error(f"❌ OHLCV取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return None

async def fetch_fgi_data() -> float:
    """
    Fear & Greed Index (FGI) を取得し、スコアリングに利用できるプロキシ値 (±0.05) に変換する。
    """
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if not data or not data.get('data'):
            logging.warning("⚠️ FGIデータが空または不正です。デフォルト値 (0.0) を使用します。")
            return 0.0

        fgi_value = int(data['data'][0]['value'])
        fgi_value_classification = data['data'][0]['value_classification']
        
        # FGI値 (0-100) を -0.5 から +0.5 のレンジに線形変換 (0.5を引いて100で割る)
        # 0 (Extreme Fear) -> -0.5
        # 50 (Neutral) -> 0.0
        # 100 (Extreme Greed) -> +0.5
        fgi_proxy_raw = (fgi_value - 50) / 100.0
        
        # スコアリングボーナス/ペナルティとして使用するため、FGI_PROXY_BONUS_MAX (0.05) にスケーリング
        fgi_proxy = fgi_proxy_raw * 2 * FGI_PROXY_BONUS_MAX # -0.05 から +0.05
        
        # グローバルコンテキストを更新
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value} ({fgi_value_classification})"
        
        logging.info(f"✅ FGI取得完了: {fgi_value} ({fgi_value_classification}) -> Proxy: {fgi_proxy:+.4f}")
        
        return fgi_proxy

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI取得失敗 - リクエストエラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGI取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return 0.0

# ====================================================================================
# CORE LOGIC: TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def apply_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrameにテクニカル指標 (SMA, EMA, MACD, RSI, ATR, BB, OBV) を追加する。
    """
    
    if df.empty:
        return df

    # 1. シンプル移動平均線 (SMA) - 長期トレンド判断用
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 

    # 2. MACD (Moving Average Convergence Divergence)
    macd_results = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = df.join(macd_results) # MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9

    # 3. RSI (Relative Strength Index)
    df['RSI_14'] = ta.rsi(df['close'], length=14)

    # 4. ATR (Average True Range) - ボラティリティ/SL計算用
    df['ATR_14'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # 5. Bollinger Bands (BB) - ボラティリティペナルティ/トレンド確認用
    bbands = ta.bbands(df['close'], length=20, std=2)
    df = df.join(bbands) # BBL_20_2.0, BBMW_20_2.0, BBU_20_2.0, BBB_20_2.0
    
    # 6. OBV (On-Balance Volume) - 出来高確証用
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # 7. その他の指標があればここに追加...
    
    # 指標計算に必要な期間のNaN行を除去
    df.dropna(inplace=True)

    return df


def determine_signal_side(df: pd.DataFrame, latest: Dict[str, float]) -> str:
    """
    テクニカル指標に基づき、取引の方向性 (long/short/neutral) を決定する。
    ここでは、主要な中期的なモメンタムに基づいて方向性を決定する。
    """
    
    if df.empty or len(df) < 2:
        return 'neutral'
        
    # 最新のMACDヒストグラム (MACDh_12_26_9) の値
    macd_h_latest = latest.get('MACDh_12_26_9', 0.0)
    # 1つ前のMACDヒストグラムの値 (MACDの勢いの変化を見る)
    macd_h_prev = df['MACDh_12_26_9'].iloc[-2] if len(df) >= 2 else 0.0
    
    # 最新のRSI
    rsi_latest = latest.get('RSI_14', 50.0)

    # 1. MACDヒストグラムの方向性 (最も強いモメンタムシグナル)
    if macd_h_latest > 0 and macd_h_latest > macd_h_prev: # MACDhがプラス圏で加速
        return 'long'
    elif macd_h_latest < 0 and macd_h_latest < macd_h_prev: # MACDhがマイナス圏で加速
        return 'short'
    
    # 2. RSIによる補助判定 (MACDが弱い場合)
    elif rsi_latest > 60:
        # MACDは弱いが増勢が強く、RSIが買われすぎ水準に近い
        return 'long'
    elif rsi_latest < 40:
        # MACDは弱いか、RSIが売られすぎ水準に近い
        return 'short'
        
    # 3. それ以外 (中立)
    return 'neutral'


def calculate_trade_score(
    df: pd.DataFrame, 
    ticker_info: Dict[str, Any], 
    side: str, 
    macro_context: Dict, 
    current_time: float
) -> Dict[str, Any]:
    """
    指定されたデータ、方向性に基づき、総合取引スコア (0.0 - 1.0) を計算する。
    スコアリングはロングとショートの両方で実行し、より高いスコアを採用する。
    """
    
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        return {'score': 0.0, 'side': 'neutral', 'long_score': BASE_SCORE, 'short_score': BASE_SCORE, 'tech_data': {}}
        
    latest = df.iloc[-1].to_dict()
    
    # スコア初期化
    score_long = BASE_SCORE # 0.40
    score_short = BASE_SCORE # 0.40
    
    # テクニカルデータ格納用
    tech_data = {}
    
    # --- スコアリングロジックの開始 ---
    
    # 1. 長期トレンドとの一致 (順張りボーナス/逆張りペナルティ)
    trend_val = 0.0
    if latest['close'] > latest['SMA_200']:
        # 価格が200SMAより上: ロング優位
        score_long += LONG_TERM_REVERSAL_PENALTY # +0.20
        score_short -= LONG_TERM_REVERSAL_PENALTY # -0.20
        trend_val = LONG_TERM_REVERSAL_PENALTY
    elif latest['close'] < latest['SMA_200']:
        # 価格が200SMAより下: ショート優位
        score_long -= LONG_TERM_REVERSAL_PENALTY # -0.20
        score_short += LONG_TERM_REVERSAL_PENALTY # +0.20
        trend_val = -LONG_TERM_REVERSAL_PENALTY
        
    # 2. MACDシグナルとの方向性一致 (モメンタム確証)
    macd_val = 0.0
    if latest.get('MACDh_12_26_9', 0.0) > 0:
        # MACDヒストグラムがプラス: ロング優位
        score_long += MACD_CROSS_PENALTY # +0.15
        score_short -= MACD_CROSS_PENALTY # -0.15
        macd_val = MACD_CROSS_PENALTY
    elif latest.get('MACDh_12_26_9', 0.0) < 0:
        # MACDヒストグラムがマイナス: ショート優位
        score_long -= MACD_CROSS_PENALTY # -0.15
        score_short += MACD_CROSS_PENALTY # +0.15
        macd_val = -MACD_CROSS_PENALTY
        
    # 3. ボラティリティ過熱ペナルティ (BB幅が平均価格の1%以上)
    # BB幅 (BBB_20_2.0) を終値で割った比率
    volatility_penalty = 0.0
    bb_ratio = latest.get('BBB_20_2.0', 0.0) / 100.0 # BBBはパーセント表示のため100で割る
    
    if bb_ratio > VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 0.01 (1%)
        penalty = -0.05 * (bb_ratio / VOLATILITY_BB_PENALTY_THRESHOLD) # 過熱度に応じてペナルティ増加
        penalty = max(-0.15, penalty) # 最大ペナルティ -0.15に制限
        score_long += penalty
        score_short += penalty
        volatility_penalty = penalty
        
    # 4. FGIマクロコンテキスト (全銘柄共通)
    fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy_bonus > 0:
        # 恐怖指数が貪欲に傾斜: ロングにボーナス、ショートにペナルティ
        score_long += fgi_proxy_bonus
        score_short -= fgi_proxy_bonus
    elif fgi_proxy_bonus < 0:
        # 恐怖指数が恐怖に傾斜: ショートにボーナス、ロングにペナルティ
        score_long += fgi_proxy_bonus # マイナス値を加算 -> ペナルティ
        score_short -= fgi_proxy_bonus # マイナス値を減算 -> ボーナス
        
    # 5. 構造的優位性ボーナス (ベースとして常に加算)
    score_long += STRUCTURAL_PIVOT_BONUS # +0.05
    score_short += STRUCTURAL_PIVOT_BONUS # +0.05
    
    # ----------------------------------------------------
    # 💥 【修正ロジックを挿入】RSIとOBVのモメンタムを反映させる
    # ----------------------------------------------------
    
    final_signal = 'long' if score_long > score_short else ('short' if score_short > score_long else 'neutral')
    
    # 6. RSIモメンタム加速ボーナス
    # 💡 【新規ロジック】RSI 40-60レンジ内での方向性加速をボーナスとする
    rsi_value = latest.get('RSI_14', 50.0)
    rsi_bonus = 0.0

    # RSI_MOMENTUM_LOW = 40 (Constants sectionで定義済み)
    if final_signal == 'long':
        # ロングシグナルでRSIが40以上かつ60未満 (モメンタム加速に適した水準)
        if RSI_MOMENTUM_LOW <= rsi_value < 60:
            rsi_bonus = 0.03 # 小さめのボーナス
    elif final_signal == 'short':
        # ショートシグナルでRSIが40超かつ60以下 (モメンタム加速に適した水準)
        if 40 < rsi_value <= (100 - RSI_MOMENTUM_LOW): # 100-40=60
            rsi_bonus = 0.03 # 小さめのボーナス

    # ロング/ショートのシグナル方向が一致するスコアにのみ加算
    if final_signal == 'long':
        score_long += rsi_bonus
    elif final_signal == 'short':
        score_short += rsi_bonus


    # 7. OBV出来高確証ボーナス (OBVトレンドの方向性を確認)
    # 💡 【新規ロジック】OBVが50MAを上回っているか（ロング）/下回っているか（ショート）で判断
    obv_value = latest.get('OBV', np.nan)
    # 最新のバーを除いた過去50期間の平均を計算
    obv_50_avg = df['OBV'].iloc[-50:-1].mean() if len(df) >= 50 and 'OBV' in df.columns else np.nan
    obv_bonus = 0.0
    
    if not np.isnan(obv_value) and not np.isnan(obv_50_avg):
        # OBVが現時点の平均より大きい
        if obv_value > obv_50_avg:
            # final_signalが'long'の場合のみボーナス適用 (出来高による買い確証)
            if final_signal == 'long':
                 obv_bonus = OBV_MOMENTUM_BONUS
        # OBVが現時点の平均より小さい
        elif obv_value < obv_50_avg:
            # final_signalが'short'の場合のみボーナス適用 (出来高による売り確証)
            if final_signal == 'short':
                 obv_bonus = OBV_MOMENTUM_BONUS
                 
    # final_signalに一致するスコアにのみボーナスを加算
    score_long += obv_bonus if final_signal == 'long' else 0.0
    score_short += obv_bonus if final_signal == 'short' else 0.0
    
    # ----------------------------------------------------
    
    # 8. 流動性ボーナス (出来高がTOP銘柄であるか)
    # (ここではロジックが未実装のため、いったん0.0のまま)
    liquidity_bonus_value = 0.0
    
    
    # --- 最終スコア決定 ---
    
    final_score = max(score_long, score_short)
    final_side = 'long' if final_score == score_long and final_score > BASE_SCORE else \
                 ('short' if final_score == score_short and final_score > BASE_SCORE else 'neutral')

    if final_side == 'neutral':
        final_score = BASE_SCORE # 最低スコアに戻す
    
    # スコアを最大値1.0にクリップ
    final_score = min(1.0, final_score)
    
    # tech_dataの更新 (通知用)
    tech_data.update({
        'long_term_reversal_penalty_value': trend_val,
        'macd_penalty_value': macd_val,
        
        # 💥 修正: 計算した値を反映
        'rsi_momentum_bonus_value': rsi_bonus, 
        'obv_momentum_bonus_value': obv_bonus, 
        
        'liquidity_bonus_value': liquidity_bonus_value, # 出来高TOP銘柄ロジックは省略されているため一旦0のまま
        'sentiment_fgi_proxy_bonus': fgi_proxy_bonus,
        'volatility_penalty_value': volatility_penalty,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'raw_long_score': score_long,
        'raw_short_score': score_short,
        'latest_close': latest.get('close', 0.0),
        'latest_atr': latest.get('ATR_14', 0.0),
        'latest_rsi': latest.get('RSI_14', 50.0),
        'latest_macd_h': latest.get('MACDh_12_26_9', 0.0),
        'latest_bb_ratio': bb_ratio,
        'obv_50_avg': obv_50_avg,
    })
    
    return {
        'score': final_score,
        'side': final_side,
        'long_score': score_long,
        'short_score': score_short,
        'tech_data': tech_data
    }


# ... (以降の関数は省略しますが、ファイル全体に含まれます)

# ====================================================================================
# ATR BASED RISK MANAGEMENT (SL/TP CALCULATION)
# ====================================================================================

def calculate_risk_management_levels(
    df: pd.DataFrame, 
    side: str, 
    rr_ratio: float = RR_RATIO_TARGET, 
    min_risk_percent: float = MIN_RISK_PERCENT
) -> Dict[str, float]:
    """
    ATRに基づき、エントリー価格、ストップロス (SL)、テイクプロフィット (TP) の水準を計算する。
    """
    
    if df.empty or 'ATR_14' not in df.columns:
        return {'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'risk_usdt': 0.0, 'rr_ratio': 0.0}

    latest = df.iloc[-1]
    entry_price = latest['close']
    atr_value = latest['ATR_14']
    
    # 1. SL (ストップロス) 幅の計算
    # SL幅 = ATR * MULTIPLIER (例: 2.0 * ATR)
    risk_atr = atr_value * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (価格の0.8%)
    min_risk_absolute = entry_price * min_risk_percent
    
    # 最終的なリスク幅: ATRベースと最低リスクの大きい方
    risk_absolute = max(risk_atr, min_risk_absolute)
    
    # 2. TP (テイクプロフィット) 幅の計算
    # TP幅 = リスク幅 * RR比率 (例: 1.5)
    reward_absolute = risk_absolute * rr_ratio
    
    stop_loss = 0.0
    take_profit = 0.0
    
    if side == 'long':
        # ロングの場合: SLは下、TPは上
        stop_loss = entry_price - risk_absolute
        take_profit = entry_price + reward_absolute
    elif side == 'short':
        # ショートの場合: SLは上、TPは下
        stop_loss = entry_price + risk_absolute
        take_profit = entry_price - reward_absolute
    
    # 価格が0以下にならないように保証
    stop_loss = max(0.0, stop_loss)
    take_profit = max(0.0, take_profit)
    
    # SL/TPが計算できない、または価格差が小さすぎる場合は無効とする
    if stop_loss == 0.0 or take_profit == 0.0:
        return {'entry_price': entry_price, 'stop_loss': 0.0, 'take_profit': 0.0, 'risk_usdt': 0.0, 'rr_ratio': 0.0}

    # 最終的な実現RR比率 (計算されたTP幅 / 計算されたSL幅)
    calculated_rr_ratio = reward_absolute / risk_absolute if risk_absolute > 0 else 0.0
    
    # 名目ロット (FIXED_NOTIONAL_USDT) から見た推定リスク額 (USDT)
    # リスク額 = ロット * (SL幅 / EntryPrice) * レバレッジ (未約定のため、概算リスク)
    sl_ratio = risk_absolute / entry_price if entry_price > 0 else 0.0
    estimated_risk_usdt = FIXED_NOTIONAL_USDT * sl_ratio * LEVERAGE
    
    return {
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'risk_usdt': estimated_risk_usdt, # 概算リスク額
        'rr_ratio': calculated_rr_ratio
    }


# ====================================================================================
# CORE LOGIC: SIGNAL GENERATION & EXECUTION
# ====================================================================================

async def process_entry_signal(symbol: str, timeframe: str, score_data: Dict, current_threshold: float, ticker_info: Dict[str, Any]) -> Optional[Dict]:
    """
    スコアが閾値を超えた場合、取引を実行し、結果を返す。
    """
    
    global EXCHANGE_CLIENT, LAST_SIGNAL_TIME, TEST_MODE, LEVERAGE, OPEN_POSITIONS
    
    final_score = score_data['score']
    final_side = score_data['side']
    
    if final_score < current_threshold or final_side == 'neutral':
        return None # 閾値未満または中立のためスキップ

    # 1. クールダウンチェック
    last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
    current_time = time.time()
    
    if current_time - last_trade_time < TRADE_SIGNAL_COOLDOWN:
        logging.info(f"ℹ️ {symbol} - クールダウン期間中のためスキップします。")
        return None
        
    # 2. ポジションチェック
    if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
        logging.warning(f"⚠️ {symbol} - 既にポジションがあるため、新規エントリーをスキップします。")
        return None
        
    # 3. OHLCVデータを再取得し、ATRレベルを計算 (最新のデータで計算し直す)
    df = await fetch_ohlcv(symbol, timeframe, REQUIRED_OHLCV_LIMITS[timeframe])
    if df is None:
        logging.error(f"❌ {symbol} - SL/TP計算のためのOHLCVデータ取得失敗。取引をスキップします。")
        return None
        
    # 最新のテクニカルを再計算
    df = apply_technical_indicators(df) 
    if df.empty:
        logging.error(f"❌ {symbol} - SL/TP計算のためのテクニカル指標計算失敗。取引をスキップします。")
        return None
        
    # ATRレベルの計算
    risk_levels = calculate_risk_management_levels(df, final_side)
    
    entry_price = risk_levels['entry_price']
    stop_loss = risk_levels['stop_loss']
    take_profit = risk_levels['take_profit']
    rr_ratio = risk_levels['rr_ratio']
    
    if stop_loss == 0.0 or take_profit == 0.0 or rr_ratio == 0.0:
        logging.error(f"❌ {symbol} - SL/TPレベルの計算失敗または無効な水準。取引をスキップします。")
        return None
    
    # 4. 契約数量 (Contracts) の計算
    # 数量 = ロット(USD) / EntryPrice
    if entry_price <= 0:
         logging.error(f"❌ {symbol} - エントリー価格が不正です (0.0以下)。取引をスキップします。")
         return None
         
    # 契約数量 (Lot Size)
    contracts = FIXED_NOTIONAL_USDT / entry_price
    
    # 取引所のロットサイズルールに従って丸める必要がある
    lot_size = contracts
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if market:
        precision = market['precision']['amount']
        # CCXTのamount_to_precision関数は非同期でないため、同期関数として使用
        lot_size_str = EXCHANGE_CLIENT.amount_to_precision(symbol, contracts)
        try:
             lot_size = float(lot_size_str)
        except ValueError:
             logging.error(f"❌ {symbol} - ロットサイズの丸めエラー: {lot_size_str}")
             return None

    if lot_size <= 0:
        logging.error(f"❌ {symbol} - 計算されたロットサイズが0.0以下です。取引をスキップします。")
        return None
    
    # ロングの場合はロットサイズを正、ショートの場合は負にする (CCXTの仕様に依存)
    # ここでは、注文時の`side`と`amount`で処理するため、`amount`は絶対値を使用
    amount_to_trade = abs(lot_size)
    
    # 5. 清算価格の計算 (通知用)
    liquidation_price = calculate_liquidation_price(
        entry_price, 
        LEVERAGE, 
        final_side,
        MIN_MAINTENANCE_MARGIN_RATE
    )
    
    # 6. シグナル辞書の完成
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': final_score,
        'side': final_side,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': liquidation_price,
        'rr_ratio': rr_ratio,
        'contracts': lot_size, # 取引する数量
        'filled_usdt': FIXED_NOTIONAL_USDT, # 想定名目価値
        'tech_data': score_data['tech_data'],
        'timestamp': current_time,
    }
    
    # 7. Telegram通知 (取引前)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    message = format_telegram_message(signal_data, "取引シグナル", current_threshold)
    await send_telegram_notification(message)
    
    # 8. 取引実行 (TEST_MODEではスキップ)
    trade_result: Dict[str, Any] = {'status': 'error', 'error_message': 'TEST_MODE is active.'}
    
    if not TEST_MODE:
        
        # 注文方向
        order_side = 'buy' if final_side == 'long' else 'sell'
        
        try:
            logging.info(f"⏳ {symbol} - {order_side.upper()}注文を執行します。数量: {amount_to_trade:.4f} @ Market")
            
            # 成行注文の実行
            order = await EXCHANGE_CLIENT.create_order(
                symbol, 
                'market', 
                order_side, 
                amount_to_trade, 
                params={'leverage': LEVERAGE} # CCXT経由でレバレッジが適用されていることを確認
            )
            
            # 約定待ち (ccxtのfetch_orderをポーリングするか、ここでは単純な遅延でシミュレート)
            await asyncio.sleep(2) 
            
            # 注文の確認
            filled_order = await EXCHANGE_CLIENT.fetch_order(order['id'], symbol)
            
            if filled_order['status'] == 'closed' or filled_order['filled'] > 0:
                filled_amount = filled_order['filled']
                filled_price = filled_order['price'] or filled_order['average']
                filled_usdt = filled_amount * filled_price if filled_price else FIXED_NOTIONAL_USDT
                
                trade_result = {
                    'status': 'ok',
                    'order_id': filled_order['id'],
                    'filled_amount': filled_amount,
                    'filled_price': filled_price,
                    'filled_usdt': filled_usdt, # 実際の約定名目価値
                    'raw_order': filled_order,
                }
                
                # シグナルデータにも実際の結果を反映
                signal_data['filled_amount'] = filled_amount
                signal_data['entry_price'] = filled_price
                signal_data['filled_usdt'] = filled_usdt
                
                # 新しいポジション情報を作成 (OPEN_POSITIONSに格納する情報)
                new_position = {
                    'symbol': symbol,
                    'side': final_side,
                    'contracts': filled_amount if final_side == 'long' else -filled_amount, # longは正、shortは負
                    'entry_price': filled_price,
                    'filled_usdt': filled_usdt,
                    'liquidation_price': calculate_liquidation_price(filled_price, LEVERAGE, final_side, MIN_MAINTENANCE_MARGIN_RATE),
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'leverage': LEVERAGE,
                    'timestamp': filled_order.get('timestamp', current_time * 1000),
                    'order_id': filled_order['id'],
                }
                OPEN_POSITIONS.append(new_position)
                
                logging.info(f"✅ {symbol} - 注文約定成功。数量: {filled_amount:.4f} @ {format_price(filled_price)}")
                
                # SL/TP注文を設定
                await set_stop_loss_take_profit(new_position)
                
            else:
                trade_result = {
                    'status': 'error',
                    'error_message': f"注文が約定しませんでした。ステータス: {filled_order['status']}",
                    'raw_order': filled_order,
                }
                logging.error(f"❌ {symbol} - 注文が約定しませんでした。")
                
        except ccxt.ExchangeError as e:
            error_msg = f"取引所エラー: {e}"
            trade_result = {'status': 'error', 'error_message': error_msg}
            logging.error(f"❌ {symbol} - 注文エラー: {error_msg}")
        except Exception as e:
            error_msg = f"予期せぬ注文エラー: {e}"
            trade_result = {'status': 'error', 'error_message': error_msg}
            logging.error(f"❌ {symbol} - 注文エラー: {error_msg}", exc_info=True)
            
    # 9. ログと最終通知
    log_signal(signal_data, "取引シグナル", trade_result)
    
    # 実際の約定結果を反映した最終通知
    final_message = format_telegram_message(signal_data, "取引シグナル", current_threshold, trade_result)
    await send_telegram_notification(final_message)
    
    # 10. クールダウン時間を更新
    if trade_result['status'] == 'ok' or TEST_MODE:
        LAST_SIGNAL_TIME[symbol] = current_time

    # 最終的なシグナルデータと取引結果を結合して返す
    signal_data['trade_result'] = trade_result
    return signal_data


async def set_stop_loss_take_profit(position: Dict):
    """
    オープンポジションに対して、SL/TP注文を設定する。
    MEXC/Binance/Bybitの先物APIを使用する。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    contracts = abs(position['contracts']) # 数量は絶対値
    
    if stop_loss == 0.0 or take_profit == 0.0 or contracts == 0.0:
        logging.warning(f"⚠️ {symbol} のSL/TP設定スキップ: 無効な価格 ({stop_loss}, {take_profit}) または数量 ({contracts})")
        return

    # ポジションと反対の注文サイド
    sl_tp_side = 'sell' if side == 'long' else 'buy'
    
    try:
        logging.info(f"⏳ {symbol} - SL/TP注文を設定中...")
        
        # 1. ストップロス注文 (Stop Loss)
        # 成行SL注文 (CCXTの統一APIでは`stop`タイプを使用することが多い)
        # MEXCの場合、`stop_loss_price`をparamsに渡す必要がある
        sl_params = {}
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCの先物APIはトリガー価格をparamsで渡す
            sl_params = {'stopLossPrice': stop_loss}
            
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'stop', # または 'stop_market' / 'stop_limit'
            sl_tp_side,
            contracts,
            stop_loss, # トリガー価格/価格
            params=sl_params
        )
        
        # 2. テイクプロフィット注文 (Take Profit)
        tp_params = {}
        if EXCHANGE_CLIENT.id == 'mexc':
            tp_params = {'takeProfitPrice': take_profit}
            
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'take_profit', # または 'take_profit_market'
            sl_tp_side,
            contracts,
            take_profit, # トリガー価格/価格
            params=tp_params
        )
        
        # ポジション情報にSL/TP注文IDを保存 (CCXTのAPIに依存するため、ここではログのみ)
        logging.info(f"✅ {symbol} のSL/TP注文設定完了。SL ID: {sl_order['id']}, TP ID: {tp_order['id']}")
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ {symbol} のSL/TP注文エラー: {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} のSL/TP設定で予期せぬエラー: {e}", exc_info=True)


async def check_and_close_positions():
    """
    オープンポジションを巡回し、SL/TPに到達したか、手動で決済されたかをチェックする。
    ここでは、単純化のため、未約定のSL/TP注文はボット側で追跡しない。
    CCXTのfetch_positionsを使い、ポジションが閉じられたことを確認する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, TEST_MODE
    
    if not OPEN_POSITIONS:
        return
        
    if TEST_MODE:
        # TEST_MODEでは、ポジションのSL/TPをシミュレーションする必要があるが、
        # ここでは単純化し、スキップする
        return
        
    try:
        # 実際のオープンポジションを取引所から再取得
        current_status = await fetch_account_status()
        
        if current_status.get('error'):
            logging.error("❌ ポジションチェック失敗: 口座ステータス再取得エラー。")
            return
            
        # 現在取引所で開いているポジションのシンボルリスト
        current_open_symbols = [p['symbol'] for p in current_status['open_positions']]
        
        closed_positions = []
        new_open_positions = []
        
        for p in OPEN_POSITIONS:
            symbol = p['symbol']
            
            if symbol not in current_open_symbols:
                # ボットが認識しているポジションが取引所に存在しない -> 決済された
                
                # 決済されたポジションの詳細情報を取得するために、最新のOHLCVを取得
                df = await fetch_ohlcv(symbol, '1m', 2)
                if df is None or df.empty:
                    exit_price = p['entry_price'] # 決済価格が取れない場合はエントリー価格で代用
                    logging.warning(f"⚠️ {symbol} の決済価格取得失敗。エントリー価格 {exit_price} で代用します。")
                else:
                    exit_price = df.iloc[-1]['close'] # 最新の終値を決済価格とする
                    
                
                pnl_rate = 0.0
                pnl_usdt = 0.0
                exit_type = '不明' # SL/TP/手動決済/清算のいずれか

                # PnLの計算
                if p['filled_usdt'] > 0 and exit_price > 0:
                    long_pnl_rate = (exit_price / p['entry_price']) - 1.0
                    
                    if p['side'] == 'long':
                        pnl_rate = long_pnl_rate
                    else: # short
                        pnl_rate = -long_pnl_rate
                        
                    pnl_usdt = p['filled_usdt'] * pnl_rate * p['leverage']
                    
                    # 決済タイプを推定 (簡略化)
                    if p['stop_loss'] > 0 and p['take_profit'] > 0:
                        if p['side'] == 'long':
                            if abs(exit_price - p['stop_loss']) < abs(exit_price - p['take_profit']) and exit_price < p['entry_price']:
                                exit_type = 'ストップロス (SL)'
                            elif exit_price > p['take_profit']:
                                exit_type = 'テイクプロフィット (TP)'
                            elif exit_price < p['liquidation_price']:
                                exit_type = '強制清算 (Liq)'
                            else:
                                exit_type = '手動決済/不明'
                        else: # short
                            if abs(exit_price - p['stop_loss']) < abs(exit_price - p['take_profit']) and exit_price > p['entry_price']:
                                exit_type = 'ストップロス (SL)'
                            elif exit_price < p['take_profit']:
                                exit_type = 'テイクプロフィット (TP)'
                            elif exit_price > p['liquidation_price']:
                                exit_type = '強制清算 (Liq)'
                            else:
                                exit_type = '手動決済/不明'
                    else:
                        exit_type = '手動決済/不明'
                        
                
                trade_result = {
                    'status': 'closed',
                    'entry_price': p['entry_price'],
                    'exit_price': exit_price,
                    'filled_amount': abs(p['contracts']),
                    'pnl_rate': pnl_rate,
                    'pnl_usdt': pnl_usdt,
                    'exit_type': exit_type,
                    'stop_loss': p['stop_loss'],
                    'take_profit': p['take_profit'],
                }
                
                p['exit_type'] = exit_type # 通知メッセージ用
                
                # ログと通知
                log_signal(p, "ポジション決済", trade_result)
                
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                message = format_telegram_message(p, "ポジション決済", current_threshold, trade_result)
                await send_telegram_notification(message)
                
                closed_positions.append(symbol)
                
            else:
                # ポジションがまだ開いている
                
                # ポジション情報を最新のものに更新 (特に清算価格など)
                for current_p in current_status['open_positions']:
                    if current_p['symbol'] == symbol:
                        # ボットが管理しているSL/TPはそのままに、最新の取引所情報を反映
                        p['liquidation_price'] = current_p['liquidation_price']
                        p['raw_info'] = current_p.get('raw_info', p['raw_info'])
                        break
                        
                new_open_positions.append(p)
                
        # OPEN_POSITIONSを更新
        OPEN_POSITIONS = new_open_positions
        
        if closed_positions:
             logging.info(f"✅ ポジション決済完了: {', '.join(closed_positions)}")
        else:
             logging.info("ℹ️ 決済されたポジションはありませんでした。")
             
    except Exception as e:
        logging.error(f"❌ ポジションチェック中にエラーが発生しました: {e}", exc_info=True)


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def main_bot_scheduler():
    """
    メインの分析/取引スケジュールを実行する。
    """
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, LOOP_INTERVAL

    if not IS_CLIENT_READY:
        logging.critical("❌ スケジューラ停止: CCXTクライアントが初期化されていません。")
        return

    await fetch_fgi_data() # マクロデータは起動時に一度取得
    
    # 最初の起動メッセージを送信するためのステータスを取得
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 起動メッセージの通知
    startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    await send_telegram_notification(startup_message)
    
    while True:
        try:
            current_time = time.time()
            logging.info(f"--- メイン分析ループ開始 --- ({datetime.now(JST).strftime('%H:%M:%S')})")
            
            # 1. ティッカー情報更新 (出来高TOP銘柄リストを更新)
            ticker_info = await fetch_tickers()
            
            # 2. マクロコンテキスト更新 (FGI)
            await fetch_fgi_data()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 3. 全銘柄・時間足の分析
            all_signals: List[Dict] = []
            
            # 監視対象銘柄のリストをコピーして処理
            symbols_to_monitor = CURRENT_MONITOR_SYMBOLS.copy()

            for symbol in symbols_to_monitor:
                
                # 長期足から順に分析
                for timeframe in TARGET_TIMEFRAMES:
                    
                    limit = REQUIRED_OHLCV_LIMITS[timeframe]
                    df = await fetch_ohlcv(symbol, timeframe, limit)
                    
                    if df is None:
                        continue
                        
                    # テクニカル指標の適用
                    df_ta = apply_technical_indicators(df.copy())
                    
                    if df_ta.empty:
                        continue

                    # 方向性の決定 (MACD加速を主要シグナルとする)
                    side_to_check = determine_signal_side(df_ta, df_ta.iloc[-1].to_dict())
                    
                    # スコア計算
                    score_data = calculate_trade_score(df_ta, ticker_info, side_to_check, GLOBAL_MACRO_CONTEXT, current_time)
                    
                    # シグナル情報を作成
                    signal = {
                        'symbol': symbol,
                        'timeframe': timeframe,
                        'side': score_data['side'],
                        'score': score_data['score'],
                        'long_score': score_data['long_score'],
                        'short_score': score_data['short_score'],
                        'tech_data': score_data['tech_data'],
                        'timestamp': current_time,
                    }
                    all_signals.append(signal)
            
            # 4. シグナルフィルタリングと取引実行
            
            # スコアの降順でソート
            sorted_signals = sorted(all_signals, key=lambda x: x.get('score', 0.0), reverse=True)
            
            LAST_ANALYSIS_SIGNALS = sorted_signals # 定期レポート用に保存
            
            # 取引閾値を超えたトップシグナルを抽出
            tradable_signals = [
                s for s in sorted_signals 
                if s['score'] >= current_threshold and s['side'] != 'neutral'
            ][:TOP_SIGNAL_COUNT] # TOP_SIGNAL_COUNT (例: 1) に制限
            
            logging.info(f"📈 フィルタリング後、取引可能なシグナル数: {len(tradable_signals)} (閾値: {current_threshold*100:.2f}点)")

            for signal in tradable_signals:
                logging.info(f"🚀 取引実行: {signal['symbol']} ({signal['side']}) Score: {signal['score']:.2f}")
                await process_entry_signal(
                    signal['symbol'], 
                    signal['timeframe'], 
                    signal, 
                    current_threshold, 
                    ticker_info
                )
            
            # 5. 定期分析レポート通知 (閾値未満の最高スコアを通知)
            await notify_highest_analysis_score()
            
            # 6. WebShareデータ送信 (1時間に1回)
            if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                 webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'top_signals': sorted_signals[:5],
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                 }
                 await send_webshare_update(webshare_data)
                 LAST_WEBSHARE_UPLOAD_TIME = current_time

            LAST_SUCCESS_TIME = current_time
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            logging.info(f"--- メイン分析ループ終了 ---")
            
        except Exception as e:
            logging.error(f"❌ メイン分析ループで致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時は待機時間を長くする
            await asyncio.sleep(LOOP_INTERVAL * 5)
            continue
            
        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """
    オープンポジションの状態を定期的に監視する。
    """
    global MONITOR_INTERVAL, TEST_MODE
    
    await asyncio.sleep(15) # メインループの起動を待つ

    while True:
        try:
            if not TEST_MODE and IS_FIRST_MAIN_LOOP_COMPLETED:
                logging.info(f"--- ポジション監視ループ開始 --- ({datetime.now(JST).strftime('%H:%M:%S')})")
                
                # 1. ポジションの決済チェック
                await check_and_close_positions()
                
                # 2. 口座残高の再取得
                await fetch_account_status()
                
                logging.info(f"--- ポジション監視ループ終了 ---")
                
        except Exception as e:
            logging.error(f"❌ ポジション監視ループで致命的なエラーが発生しました: {e}", exc_info=True)
            
        # 次のループまで待機
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

# FastAPIアプリケーションの初期化
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
    
    # CCXTクライアントの初期化 (非同期で実行)
    client_ready = await initialize_exchange_client()
    
    if client_ready:
        # スケジューラをバックグラウンドで開始
        asyncio.create_task(main_bot_scheduler())
        asyncio.create_task(position_monitor_scheduler())
    else:
        logging.critical("❌ CCXTクライアントの初期化に失敗したため、スケジューラを起動しません。")


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    # 環境変数 PORT が設定されている場合は...
    port = int(os.getenv("PORT", 8000))
    logging.info(f"Uvicornを起動します (ポート: {port})")
    uvicorn.run("main_render (ほぼ完成形):app", host="0.0.0.0", port=port, log_level="info", reload=False)
