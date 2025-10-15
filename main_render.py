# ====================================================================================
# Apex BOT v19.0.27 - Final Integrated Build (完全版)
#
# 強化ポイント:
# 1. 【手数料考慮】ポジション取得時と決済時の往復手数料をリスク許容額計算に組み込み。
# 2. 【Net RRR】Telegram通知に手数料控除後の実質的なリスクリワード比率を表示。
# 3. 【MEXC Patch】USDT残高取得ロジックを強化し、MEXCのRaw Infoから強制抽出を試みる。
# 4. 【エラー対策】get_fx_macro_context()で外部APIエラー時にTypeErrorを防ぐロバストな処理を追加。
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
import yfinance as yf
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json # for ccxt raw info parsing

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT",
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT",
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT",
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
]
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.70            
TRADE_EXECUTION_THRESHOLD = 0.85    # これが即時通知とロング取引実行のトリガーになる
TOP_SIGNAL_COUNT = 3               
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 

# v19.0.27 リスク管理定数 (動的リスクサイジング + 手数料調整)
MAX_BASE_RISK_CAPITAL_PERCENT = 0.01    # 基準リスク率 (残高の1%)
MAX_DYNAMIC_RISK_CAPITAL_PERCENT = 0.03 # 動的リスクの最大上限 (残高の3%)
MAX_ABS_RISK_USDT_CAP = 10.0            # 絶対額での最大上限 (USDT)
# 💡 往復手数料率: 0.075% (購入) + 0.075% (売却) = 0.15% を想定
TRADING_FEE_RATE_PER_SIDE = 0.00075     # 片道手数料率 (例: 0.075%)
TRADING_FEE_RATE_ROUND_TRIP = TRADING_FEE_RATE_PER_SIDE * 2 # 往復手数料率 (例: 0.15%)


# v19.0.25 Funding/OI 定数
OI_SURGE_PENALTY = 0.10             
FR_HIGH_PENALTY = 0.08              
FR_LOW_BONUS = 0.05                 
FR_HIGH_THRESHOLD = 0.0005          
FR_LOW_THRESHOLD = -0.0005          

# v19.0.18 FX/マクロテクニカル定数
RSI_DIVERGENCE_PENALTY = 0.12        
ADX_TREND_STRENGTH_BONUS = 0.06      
ADX_TREND_ABSENCE_PENALTY = 0.07     
RSI_SUPPORT_TOUCH_BONUS = 0.05       
MULTI_PIVOT_CONFLUENCE_BONUS = 0.04  
VPVR_GAP_PENALTY = 0.08              
DXY_SURGE_PENALTY = 0.15             
USDJPY_RISK_OFF_PENALTY = 0.10       
FX_MACRO_STABILITY_BONUS = 0.07      

# v19.0.17 復元定数
LONG_TERM_REVERSAL_PENALTY = 0.25   
MACD_CROSS_PENALTY = 0.18
MACD_CROSS_BONUS = 0.07             
MACD_DIVERGENCE_PENALTY = 0.18
VWAP_BONUS_POINT = 0.05             
BB_SQUEEZE_BONUS = 0.04             
BB_WIDE_PENALTY = 0.05               
MIN_RRR_THRESHOLD = 3.0              
RRR_BONUS_MULTIPLIER = 0.04          
LIQUIDITY_BONUS_POINT = 0.06        
WHALE_IMBALANCE_PENALTY = 0.12      
WHALE_IMBALANCE_BONUS = 0.08        
WHALE_IMBALANCE_THRESHOLD = 0.60    
FGI_PROXY_BONUS_MAX = 0.10          
FGI_PROXY_PENALTY_MAX = 0.10        
RSI_OVERBOUGHT_PENALTY = 0.10        
RSI_OVERBOUGHT_THRESHOLD = 60        
RSI_MOMENTUM_LOW = 40                
BASE_SCORE = 0.40                   
RANGE_TRAIL_MULTIPLIER = 3.0        
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 
DTS_RRR_DISPLAY = 5.0                
ADX_ABSENCE_THRESHOLD = 20.0

MIN_USDT_BALANCE_TO_TRADE = 50.0    

# ====================================================================================
# GLOBAL STATE
# ====================================================================================

CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
LAST_ANALYSIS_SIGNALS: Dict[str, Any] = {}
LAST_SUCCESS_TIME: float = 0
TRADE_COOLDOWN_TIMESTAMPS: Dict[str, float] = {} # キーは "SYMBOL-SIDE"
ACTUAL_POSITIONS: Dict[str, Any] = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {}
CURRENT_USDT_BALANCE: float = 0.0
CURRENT_BALANCE_STATUS: str = "INITIALIZING"
FX_MACRO_CONTEXT: Dict = {}
FUNDING_OI_CONTEXT: Dict = {} 

EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CCXT_CLIENT_NAME: str = os.environ.get('CCXT_EXCHANGE', 'binance')
CCXT_API_KEY: str = os.environ.get('CCXT_API_KEY', 'YOUR_API_KEY')
CCXT_SECRET: str = os.environ.get('CCXT_SECRET', 'YOUR_SECRET')

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """利確までの時間目安を計算する"""
    if timeframe == '15m': return "数時間以内 (1-5本程度の15m足)"
    if timeframe == '1h': return "24時間以内 (1-5本程度の1h足)"
    if timeframe == '4h': return "数日以内 (1-3本程度の4h足)"
    return "不明"

def format_price_utility(price: float) -> str:
    """価格を適切な桁数でフォーマットする"""
    if price >= 100: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.3f}"
    if price >= 1: return f"{price:,.4f}"
    return f"{price:,.6f}"

def format_usdt(amount: float) -> str:
    """USDT残高をフォーマットする"""
    return f"{amount:,.2f}"

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間足に基づき推定勝率を計算する"""
    base_rate = score * 0.50 + 0.35 

    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate - 0.05))
    if timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    if timeframe == '4h':
        return max(0.50, min(0.90, base_rate + 0.05))
    
    return base_rate

def send_telegram_html(message: str):
    """HTML形式でTelegramにメッセージを送信する (同期的に実装)"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram設定がされていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram送信エラー: {e}")

def send_position_status_notification(message: str):
    """ポジションの状況変化をTelegramに通知する (ここではシグナル通知として代用)"""
    send_telegram_html(message)


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """分析結果を統合したTelegramメッセージをHTML形式で作成する (v19.0.27 - 手数料対応)"""

    if not signals: return ""
    best_signal = signals[0] 

    # データ抽出
    side = best_signal.get('side', 'ロング')
    timeframe = best_signal.get('timeframe', '1h')
    price = best_signal.get('entry_price', 0.0)
    score_raw = best_signal.get('score', 0.0)
    entry_price = best_signal.get('entry_price', 0.0)
    trade_plan_data = best_signal.get('trade_plan', {})
    sl_price = trade_plan_data.get('sl_price', 0.0)
    tp1_price = trade_plan_data.get('tp1_price', 0.0)
    rr_ratio_gross = best_signal.get('rr_ratio_gross', 0.0) 
    risk_amount = best_signal.get('risk_amount_usdt', 0.0)
    
    # 💡 v19.0.27: 手数料調整後のデータを抽出
    net_reward_amount = best_signal.get('net_reward_amount', 0.0)
    estimated_fee_usdt = best_signal.get('estimated_fee_usdt', 0.0)
    net_rr_ratio = best_signal.get('net_rr_ratio', 0.0)

    reward_amount_gross = risk_amount * rr_ratio_gross if risk_amount else 0.0
    
    is_tradable = score_raw >= TRADE_EXECUTION_THRESHOLD
    
    display_symbol = symbol
    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100

    # 💡 v19.0.26: 動的リスク率を抽出
    dynamic_risk_pct = best_signal.get('dynamic_risk_pct', MAX_BASE_RISK_CAPITAL_PERCENT) * 100 
    
    if score_raw >= 0.90: confidence_text = "<b>極めて高い (確信度:S)</b>"
    elif score_raw >= 0.85: confidence_text = "<b>極めて高い (確信度:A)</b>"
    elif score_raw >= 0.75: confidence_text = "<b>高い (確信度:B)</b>"
    else: confidence_text = "中程度 (確信度:C)"

    # ロング/ショートの表示を切り替え
    if side == 'ロング':
        direction_emoji = "🚀"
        direction_text = "<b>ロング (現物買い推奨)</b>"
        risk_desc = "リスク許容額"
        reward_desc_gross = "グロスリワード額"
        reward_desc_net = "実質リワード額 (Net)"
    else: # ショート
        direction_emoji = "💥"
        # 💡 現物特化のためショートは「分析情報」と明記
        direction_text = "<b>ショート (分析情報/現物買いの押し目判断)</b>" 
        risk_desc = "仮想リスク許容額"
        reward_desc_gross = "仮想グロスリワード額"
        reward_desc_net = "仮想実質リワード額 (Net)"

    header = (
        f"{direction_emoji} <b>【APEX BOT SIGNAL: {confidence_text}】</b> {direction_emoji}\n\n"
        f"<b>🥇 BOT自動取引実行シグナル</b>\n"
        f"銘柄: **{display_symbol}**\n"
        f"方向: {direction_text}\n"
        f"現在価格: <code>${format_price_utility(price)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    trade_plan = (
        f"### 🎯 <b>トレードプラン概要 ({timeframe})</b>\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price)}</code>\n"
        f"  - <b>損切り (SL)</b>: <code>${format_price_utility(sl_price)}</code>\n"
        f"    ({risk_desc}: ${format_usdt(risk_amount)})\n"
        f"  - <b>利確目標 (TP1)</b>: <code>${format_price_utility(tp1_price)}</code>\n"
        f"  - <b>グロスRRR</b>: <b>1:{rr_ratio_gross:.2f}</b>\n"
        f"  - <b>{reward_desc_gross}</b>: ${format_usdt(reward_amount_gross)}\n\n"
        
        f"  - <b>🏦 想定往復手数料</b>: <code>-${estimated_fee_usdt:.2f}</code>\n"
        f"  - <b>✨ 実質RRR (Net)</b>: <b>1:{net_rr_ratio:.2f}</b>\n"
        f"  - <b>{reward_desc_net}</b>: <code>${format_usdt(net_reward_amount)}</code>\n" 
        f"  - <b>動的リスク率</b>: <code>{dynamic_risk_pct:.2f}%</code> (残高比)\n\n" 
    )

    tech_data = best_signal.get('tech_data', {})
    regime = "強い上昇トレンド" if tech_data.get('adx', 0.0) >= 30 and tech_data.get('di_plus', 0.0) > tech_data.get('di_minus', 0.0) else ("トレンド相場" if tech_data.get('adx', 0.0) >= ADX_ABSENCE_THRESHOLD else "レンジ相場")
    fgi_score = tech_data.get('sentiment_fgi_proxy_value', 0.0) 
    fgi_sentiment = "リスクオン" if fgi_score > 0 else ("リスクオフ" if fgi_score < 0 else "中立")

    fx_bonus = tech_data.get('fx_macro_stability_bonus', 0.0)
    fx_dxy_penalty = tech_data.get('dxy_surge_penalty', 0.0)
    fx_usdjpy_penalty = tech_data.get('usdjpy_risk_off_penalty', 0.0)
    fx_status = "安定"
    if fx_bonus > 0: fx_status = "ポジティブ"
    if fx_dxy_penalty < 0 or fx_usdjpy_penalty < 0: fx_status = "リスク警戒"

    # Funding/OI要因の表示
    oi_penalty = tech_data.get('oi_surge_penalty', 0.0)
    fr_penalty = tech_data.get('fr_high_penalty', 0.0)
    fr_bonus = tech_data.get('fr_low_bonus', 0.0)
    leverage_status = "中立"
    if fr_bonus > 0: leverage_status = "ショート優勢/スクイーズ期待"
    elif fr_penalty < 0: leverage_status = "ロング過熱/調整リスク"
    if oi_penalty < 0: leverage_status = "🔥 市場過熱/清算リスク"


    summary = (
        f"<b>💡 分析サマリー</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>分析スコア</b>: <code>{score_100:.2f} / 100</code> (信頼度: {confidence_text})\n"
        f"  - <b>予測勝率</b>: <code>約 {win_rate:.1f}%</code>\n"
        f"  - <b>時間軸 (メイン)</b>: <code>{timeframe}</code>\n"
        f"  - <b>決済までの目安</b>: {get_tp_reach_time(timeframe)}\n"
        f"  - <b>市場の状況</b>: {regime} (ADX: {tech_data.get('adx', 0.0):.1f})\n"
        f"  - <b>レバレッジ/Funding</b>: {leverage_status}\n"
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n"
        f"  - <b>為替 (FX) マクロ</b>: {fx_status}\n\n"
    )

    plus_factors = []
    minus_factors = []
    
    # --- プラス要因の評価 ---
    lt_penalty = tech_data.get('long_term_reversal_penalty', False)
    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    
    # 日本語の方向を切り替え
    trend_match_desc = f"長期 ({lt_trend_str}) トレンドとの一致" if side == 'ロング' else f"長期 ({lt_trend_str}) トレンドとの逆行"
    macd_cross_desc = "MACDゴールデンクロス確証" if side == 'ロング' else "MACDデッドクロス確証"
    rsi_bounce_desc = "RSIサポート(40以下)からの反発" if side == 'ロング' else "RSIレジスタンス(60以上)からの反落"
    adx_strength_desc = f"ADXトレンド強度の裏付け ({'上昇' if side == 'ロング' else '下降'})"
    whale_bias_desc = f"🐋 クジラの{'買い' if side == 'ロング' else '売り'}圧力優位"
    fr_bias_desc = f"💰 **Funding Rateが{'ネガティブ' if side == 'ロング' else 'ポジティブ'}** ({'ショート' if side == 'ロング' else 'ロング'}清算期待)"
    
    if not lt_penalty: plus_factors.append(f"{trend_match_desc} (+{LONG_TERM_REVERSAL_PENALTY*100:.1f}点相当)") 
    if tech_data.get('macd_cross_valid', False): plus_factors.append(f"{macd_cross_desc} (+{MACD_CROSS_BONUS*100:.1f}点)")
    if tech_data.get('structural_pivot_bonus', 0.0) > 0: plus_factors.append(f"重要{'支持' if side == 'ロング' else '抵抗'}線からの反発確証 ({tech_data.get('fib_proximity_level', 'N/A')}確認)")
    if tech_data.get('multi_pivot_confluence_bonus_value', 0.0) > 0: plus_factors.append(f"🧱 多重ピボットコンフルエンス (+{MULTI_PIVOT_CONFLUENCE_BONUS*100:.1f}点)")
    if tech_data.get('rsi_support_touch_bonus', 0.0) > 0: plus_factors.append(f"📈 {rsi_bounce_desc} (+{RSI_SUPPORT_TOUCH_BONUS*100:.1f}点)")
    if tech_data.get('adx_strength_bonus_value', 0.0) > 0: plus_factors.append(f"💪 {adx_strength_bonus_value*100:.1f}点)")
    if tech_data.get('volume_confirmation_bonus', 0.0) > 0: plus_factors.append(f"出来高による裏付け (+{tech_data.get('volume_confirmation_bonus', 0.0)*100:.1f}点)")
    if tech_data.get('liquidity_bonus_value', 0.0) > 0: plus_factors.append(f"板の厚み (流動性) 優位 (+{tech_data.get('liquidity_bonus_value', 0.0)*100:.1f}点)")
    if tech_data.get('vwap_confirm_ok', False): plus_factors.append(f"価格がVWAPよりも{'上' if side == 'ロング' else '下'} ({'買い' if side == 'ロング' else '売り'}圧力優位)")
    if tech_data.get('bb_squeeze_ok', False): plus_factors.append(f"ボラティリティ圧縮後のブレイク期待 (+{BB_SQUEEZE_BONUS*100:.1f}点)")
    if tech_data.get('rrr_bonus_value', 0.0) > 0: plus_factors.append(f"優秀なRRR ({rr_ratio_gross:.2f}+) (+{tech_data.get('rrr_bonus_value', 0.0)*100:.1f}点)")
    if tech_data.get('whale_imbalance_bonus', 0.0) > 0: plus_factors.append(f"{whale_bias_desc} (+{WHALE_IMBALANCE_BONUS*100:.1f}点)")
    if fx_bonus > 0: plus_factors.append(f"💸 FX市場の安定 (+{FX_MACRO_STABILITY_BONUS*100:.1f}点)")
    if fr_bonus > 0: plus_factors.append(f"{fr_bias_desc} (+{FR_LOW_BONUS*100:.1f}点)")
    if fgi_score > 0 and side == 'ロング': plus_factors.append(f"😨 FGIプロキシがリスクオンを示唆 (+{fgi_score*100:.1f}点)")
    if fgi_score < 0 and side == 'ショート': plus_factors.append(f"😨 FGIプロキシがリスクオフを示唆 (+{abs(fgi_score)*100:.1f}点)")


    # --- マイナス要因の評価 ---
    trend_penalty_desc = f"長期トレンド ({lt_trend_str}) と{'逆行' if side == 'ロング' else '一致'}"
    macd_cross_pen_desc = f"MACDが{'デッド' if side == 'ロング' else 'ゴールデン'}クロス状態"
    rsi_div_desc = f"⚠️ RSI{'弱気' if side == 'ロング' else '強気'}ダイバージェンス"
    rsi_overbought_desc = f"RSI過熱 (RSI {'60以上' if side == 'ロング' else '40以下'})"
    stoch_pen_desc = f"ストキャスティクス過熱 ({'買われすぎ80' if side == 'ロング' else '売られすぎ20'}以上)"
    vpvr_pen_desc = f"🚧 出来高空隙 (SLまでの急{'落' if side == 'ロング' else '騰'}リスク)"
    whale_imbalance_pen_desc = f"🐋 クジラの{'売り' if side == 'ロング' else '買い'}圧力優位"
    fr_high_pen_desc = f"💰 **Funding Rateが{'ポジティブ' if side == 'ロング' else 'ネガティブ'}** ({'ロング' if side == 'ロング' else 'ショート'}過熱)"


    if lt_penalty: minus_factors.append(f"{trend_penalty_desc} (-{LONG_TERM_REVERSAL_PENALTY*100:.1f}点)")
    if tech_data.get('macd_cross_valid', False) == False and tech_data.get('macd_divergence_penalty', False) == False: minus_factors.append(f"{macd_cross_pen_desc} (-{MACD_CROSS_PENALTY*100:.1f}点)")
    if tech_data.get('macd_divergence_penalty', False): minus_factors.append(f"MACDダイバージェンスの兆候 (トレンド終焉リスク) (-{MACD_DIVERGENCE_PENALTY*100:.1f}点)")
    if tech_data.get('rsi_divergence_penalty', False): minus_factors.append(f"{rsi_div_desc} (-{RSI_DIVERGENCE_PENALTY*100:.1f}点)")
    if tech_data.get('rsi_overbought_penalty', 0) > 0: minus_factors.append(f"{rsi_overbought_desc} (-{RSI_OVERBOUGHT_PENALTY*100:.1f}点)") 
    if tech_data.get('stoch_filter_penalty', 0) > 0: minus_factors.append(f"{stoch_pen_desc} (-{0.15*100:.1f}点)") 
    if tech_data.get('adx_absence_penalty', 0.0) < 0: minus_factors.append(f"📉 ADXトレンド不在 (ADX < 20 / レンジリスク) (-{ADX_TREND_ABSENCE_PENALTY*100:.1f}点)")
    if tech_data.get('oi_surge_penalty', 0.0) < 0: minus_factors.append(f"🔥 **建玉 (OI) の急増** (市場過熱リスク) (-{OI_SURGE_PENALTY*100:.1f}点)")
    if tech_data.get('vpvr_gap_penalty', 0.0) < 0: minus_factors.append(f"{vpvr_pen_desc} (-{VPVR_GAP_PENALTY*100:.1f}点)")
    if tech_data.get('bb_wide_penalty', False): minus_factors.append(f"ボラティリティ過大 (BB幅 > 8.0%) (-{BB_WIDE_PENALTY*100:.1f}点)")
    if tech_data.get('whale_imbalance_penalty', 0.0) < 0: minus_factors.append(f"{whale_imbalance_pen_desc} (-{WHALE_IMBALANCE_PENALTY*100:.1f}点)")
    if fx_dxy_penalty < 0 and side == 'ロング': minus_factors.append(f"⚠️ DXY急騰 (グローバルリスクオフ) (-{DXY_SURGE_PENALTY*100:.1f}点)")
    if fx_usdjpy_penalty < 0 and side == 'ロング': minus_factors.append(f"🚨 USD/JPY急激な円高 (資金逃避リスク) (-{USDJPY_RISK_OFF_PENALTY*100:.1f}点)")
    if fr_penalty < 0 and side == 'ロング': minus_factors.append(f"{fr_high_pen_desc} (-{FR_HIGH_PENALTY*100:.1f}点)")
    if fr_low_bonus > 0 and side == 'ショート': minus_factors.append(f"💰 Funding Rateがネガティブ (ショート清算リスク) (-{FR_LOW_BONUS*100:.1f}点)")
    if fgi_score < 0 and side == 'ロング': minus_factors.append(f"😨 FGIプロキシがリスクオフを示唆 (-{FGI_PROXY_PENALTY_MAX*100:.1f}点)")
    if fgi_score > 0 and side == 'ショート': minus_factors.append(f"😨 FGIプロキシがリスクオンを示唆 (-{FGI_PROXY_PENALTY_MAX*100:.1f}点)")


    # プラス要因セクション
    plus_section = (f"<b>📊 分析の確証 (高得点要因)</b>\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n")
    for factor in plus_factors: plus_section += f"  ✅ {factor}\n"
    if not plus_factors: plus_section += f"  <i>(高得点要因は特にありません)</i>\n"
    plus_section += "\n"

    # マイナス要因セクション
    minus_section = (f"<b>🚨 懸念/ペナルティ要因 (マイナス)</b>\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n")
    for factor in minus_factors: minus_section += f"  ❌ {factor}\n"
    if not minus_factors: minus_section += f"  <i>(目立ったリスク要因はありません)</i>\n"
    minus_section += "\n"

    # 💡 修正箇所: フッターの自動売買対象表示
    footer_text = ""
    if side == 'ロング':
         footer_text = f"※ このシグナルは自動売買の{'対象です。' if is_tradable and CURRENT_USDT_BALANCE >= MIN_USDT_BALANCE_TO_TRADE else '対象外です。（残高不足またはスコア不足）'}"
    else:
         footer_text = "※ ショートシグナルのため、自動売買（現物）は実行されません。"


    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>{footer_text}</pre>"
        f"<i>Bot Ver: v19.0.27 (Final Integrated Build)</i>"
    )

    return header + trade_plan + summary + plus_section + minus_section + footer

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, CCXT_CLIENT_NAME
    
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME)
    # 💡 現物特化のため defaultType: 'spot' を維持
    EXCHANGE_CLIENT = exchange_class({'apiKey': CCXT_API_KEY, 'secret': CCXT_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
    logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で初期化しました。")

async def fetch_current_balance_usdt_with_status(client: ccxt_async.Exchange) -> Tuple[float, str]:
    """現在のUSDT現物残高を取得し、ステータスも返す (MEXC Raw Info Patch対応)"""
    status = "OK"
    usdt_free = 0.0
    try:
        if not client: 
            return 0.0, "CLIENT_UNINITIALIZED"
        
        balance_data = await client.fetch_balance()
        
        # 1. 標準的なCCXT形式からの抽出
        if 'USDT' in balance_data.get('free', {}):
            usdt_free = balance_data['free']['USDT']
        elif 'USDT' in balance_data.get('total', {}):
             usdt_free = balance_data['total']['USDT'] # freeが使えない場合のフォールバック

        # 2. 💡 MEXC特有の Raw Info パッチ: 'info' 内の 'assets' から USDT free を探す
        if usdt_free == 0.0 and CCXT_CLIENT_NAME == 'mexc' and 'info' in balance_data and 'assets' in balance_data['info']:
            for asset in balance_data['info']['assets']:
                if asset.get('assetName') == 'USDT':
                    usdt_free = float(asset.get('availableBalance', 0.0))
                    logging.warning("MEXC Raw Info PatchによりUSDT残高を強制抽出しました。")
                    break
        
        usdt_free = float(usdt_free)

        if usdt_free <= 0.0:
             status = "ZERO_BALANCE"
             usdt_free = 0.0
        elif usdt_free < MIN_USDT_BALANCE_TO_TRADE:
             status = "LOW_BALANCE"
        else:
             status = "TRADABLE"

        return usdt_free, status
        
    except Exception as e:
        logging.error(f"USDT残高取得エラー: {e}", exc_info=True)
        status = "BALANCE_FETCH_ERROR"
        return 0.0, status

async def update_symbols_by_volume(client: ccxt_async.Exchange):
    """出来高に基づいて監視銘柄リストを更新する (簡易版)"""
    global CURRENT_MONITOR_SYMBOLS
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
    logging.info(f"監視銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄")
        
async def fetch_ohlcv_with_fallback(exchange_name: str, symbol: str, timeframe: str) -> Tuple[Optional[pd.DataFrame], str, Optional[ccxt.Exchange]]:
    """OHLCVデータを取得し、失敗時にフォールバックする (簡易シミュレーションを維持)"""
    global EXCHANGE_CLIENT
    
    try:
        if not EXCHANGE_CLIENT: return None, "Error: Client not initialized", None

        # シミュレーションデータを作成
        limit = REQUIRED_OHLCV_LIMITS[timeframe]
        data = np.random.rand(limit, 6)
        base_price = 65000 + (random.random() * 5000 - 2500)
        data[:, 0] = np.arange(time.time() - (limit * 3600), time.time(), 3600) 
        data[:, 1] = base_price + np.random.normal(0, 100, limit)
        data[:, 4] = data[:, 1] + np.random.normal(0, 100, limit)
        data[:, 2] = np.maximum(data[:, 1], data[:, 4]) + np.random.uniform(0, 50, limit)
        data[:, 3] = np.minimum(data[:, 1], data[:, 4]) - np.random.uniform(0, 50, limit)
        data[:, 5] = np.random.uniform(5000, 20000, limit) 

        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        
        return df, "Success", EXCHANGE_CLIENT

    except Exception as e:
        return None, f"Error: {e}", EXCHANGE_CLIENT

async def fetch_order_book_depth(symbol: str):
    """オーダーブック（板情報）を取得し、キャッシュに保存する (簡易版)"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE
    if not EXCHANGE_CLIENT: return

    try:
        # シミュレーションデータ
        if symbol == "BTC/USDT":
             price = 65000.0 + random.uniform(-100, 100)
        elif symbol == "ETH/USDT":
             price = 3500.0 + random.uniform(-50, 50)
        else:
             price = 100.0 + random.uniform(-5, 5)

        bids = [[price - 0.0005 * price * i, 10 + 2 * i] for i in range(1, 10)]
        asks = [[price + 0.0005 * price * i, 10 + 3 * i] for i in range(1, 10)]
        
        orderbook = {'bids': bids, 'asks': asks}
        ORDER_BOOK_CACHE[symbol] = orderbook
        
    except Exception as e:
        ORDER_BOOK_CACHE[symbol] = None

async def get_fx_macro_context() -> Dict:
    """為替市場データ（USD/JPY, DXY）を取得し、マクロセンチメントを計算する (エラーハンドリング強化)"""
    
    fx_data = {'sentiment_fgi_proxy_value': 0.0, 'dxy_surge_penalty': 0.0, 'usdjpy_risk_off_penalty': 0.0, 'usdjpy_stability_bonus': 0.0}
    
    try:
        # 1. DXY (USD Index)
        dxy_data = yf.download('DX-Y.NYB', period='5d', interval='1d', progress=False, timeout=10)
        if not dxy_data.empty and len(dxy_data) >= 2:
            dxy_current = dxy_data['Close'].iloc[-1]
            dxy_prev = dxy_data['Close'].iloc[-2]
            dxy_change_pct = (dxy_current - dxy_prev) / dxy_prev
            
            # DXYが急騰した場合 (リスクオフ要因)
            if dxy_change_pct > 0.005: 
                fx_data['dxy_surge_penalty'] = -DXY_SURGE_PENALTY

        # 2. USD/JPY
        usdjpy_data = yf.download('USDJPY=X', period='5d', interval='1d', progress=False, timeout=10)
        if not usdjpy_data.empty and len(usdjpy_data) >= 2:
            usdjpy_current = usdjpy_data['Close'].iloc[-1]
            usdjpy_prev = usdjpy_data['Close'].iloc[-2]
            usdjpy_change_pct = (usdjpy_current - usdjpy_prev) / usdjpy_prev
            
            # USD/JPYが急激に円高になった場合 (リスクオフ要因)
            if usdjpy_change_pct < -0.005: 
                fx_data['usdjpy_risk_off_penalty'] = -USDJPY_RISK_OFF_PENALTY
            # USD/JPYが安定している場合 (リスクオン要因)
            elif abs(usdjpy_change_pct) < 0.002:
                fx_data['usdjpy_stability_bonus'] = FX_MACRO_STABILITY_BONUS

    except Exception as e:
        # 💡 NoneTypeエラーを回避するためのエラーハンドリング
        logging.warning(f"マクロコンテキスト取得中にエラー発生（外部API）：{e}", exc_info=True)
        logging.warning("⚠️ マクロコンテキストの取得が失敗しました。デフォルト値で初期化を続行します。")
        # デフォルト値が返されるため、TypeErrorは発生しない

    # 3. FGIプロキシ (簡易版を維持)
    btc_volatility_proxy = random.uniform(0.1, 0.8) 
    if btc_volatility_proxy > 0.65:
        fx_data['sentiment_fgi_proxy_value'] = -FGI_PROXY_PENALTY_MAX * random.uniform(0.5, 1.0) # 恐怖
    elif btc_volatility_proxy < 0.25:
        fx_data['sentiment_fgi_proxy_value'] = FGI_PROXY_BONUS_MAX * random.uniform(0.5, 1.0) # 楽観
        
    return fx_data

async def get_funding_oi_context(symbol: str) -> Dict:
    """Funding RateとOpen Interestのデータを取得し分析する (シミュレーションを維持)"""
    
    context = {}
    try:
        # 1. Funding Rate (FR) シミュレーション
        funding_rate = random.choice([0.0006, 0.0001, -0.0001, -0.0006, 0.0003, -0.0003])
        
        if funding_rate > FR_HIGH_THRESHOLD:
            context['fr_high_penalty'] = -FR_HIGH_PENALTY
        elif funding_rate < FR_LOW_THRESHOLD:
            context['fr_low_bonus'] = FR_LOW_BONUS
            
        # 2. Open Interest (OI) 急増シミュレーション
        oi_change_percent = random.uniform(-0.05, 0.15) 
        
        if oi_change_percent > 0.10: # 10%以上のOI急増をペナルティ化
            context['oi_surge_penalty'] = -OI_SURGE_PENALTY
            
    except Exception as e:
        logging.warning(f"Funding/OIデータ取得エラー: {e}")
        pass
        
    return context


# ====================================================================================
# ANALYSIS CORE (v19.0.27 強化版 - Fee-Adjusted)
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算する"""
    df = df.copy()
    # SMA/EMA, MACD, RSI, Stochastics, BBANDS, VWAP, ATR, ADX/DI, VPVRプロキシの計算
    df.ta.sma(length=50, append=True)
    df.ta.ema(length=200, append=True)
    macd = df.ta.macd(append=True)
    df['MACDh_12_26_9'] = macd['MACDh_12_26_9']
    df.ta.rsi(length=14, append=True)
    stoch = df.ta.stoch(append=True)
    df['STOCHk_14_3_3'] = stoch['STOCHk_14_3_3']
    df['STOCHd_14_3_3'] = stoch['STOCHd_14_3_3']
    bbands = df.ta.bbands(append=True)
    df['BBL_5_2.0'] = bbands['BBL_5_2.0']
    df['BBU_5_2.0'] = bbands['BBU_5_2.0']
    df['VWAP'] = df.ta.vwap(append=False)
    df['AvgRange'] = df.ta.atr(length=14)
    adx_data = df.ta.adx(length=14)
    df['ADX'] = adx_data['ADX_14']
    df['DMP'] = adx_data['DMP_14'] # +DI
    df['DMN'] = adx_data['DMN_14'] # -DI
    df['Volume_Avg_50'] = df['volume'].rolling(50).mean()
    # 簡易的なVPVR Gap Proxy
    df['VPVR_Gap_Proxy'] = df.apply(lambda row: row['Volume_Avg_50'] * 0.1 > row['volume'] if row['Volume_Avg_50'] else False, axis=1)

    return df.dropna().reset_index(drop=True)

def calculate_risk_reward_ratio(entry: float, sl: float, tp1: float, side: str) -> float:
    """リスクリワード比率を計算する (ショート対応)"""
    if side == 'ロング':
        risk = entry - sl
        reward = tp1 - entry
    else: # ショート
        risk = sl - entry
        reward = entry - tp1

    return round(reward / risk, 2) if risk > 0 else 0.0

def get_fibonacci_level(high: float, low: float, level: float) -> float:
    """フィボナッチリトレースメントレベルを計算する"""
    return low + (high - low) * level

def check_rsi_divergence(df: pd.DataFrame, current_price: float, side: str) -> bool:
    """RSIダイバージェンス（弱気/強気）をチェックする (簡易版)"""
    
    if len(df) < 50: return False

    rsi = df['RSI_14'].iloc[-20:]
    close = df['close'].iloc[-20:]

    if side == 'ロング':
        # 弱気ダイバージェンス (価格上昇、RSI低下)
        peak_index = close.idxmax()
        if peak_index == close.index[0]: return False 
        prev_close = close.loc[:peak_index - 1]
        if prev_close.empty: return False
        prev_peak_index = prev_close.idxmax()
        
        price_higher = close.loc[peak_index] > close.loc[prev_peak_index]
        rsi_lower = rsi.loc[peak_index] < rsi.loc[prev_peak_index]
        
        if price_higher and rsi_lower:
            return True 
    
    else: # ショート
        # 強気ダイバージェンス (価格低下、RSI上昇)
        trough_index = close.idxmin()
        if trough_index == close.index[0]: return False 
        prev_close = close.loc[:trough_index - 1]
        if prev_close.empty: return False
        prev_trough_index = prev_close.idxmin()

        price_lower = close.loc[trough_index] < close.loc[prev_trough_index]
        rsi_higher = rsi.loc[trough_index] > rsi.loc[prev_trough_index]

        if price_lower and rsi_higher:
             return True
            
    return False

def get_multi_pivot_confluence(sl_price: float, fib_levels: Dict, current_price: float, side: str) -> float:
    """SL近辺の多重ピボット/Fibコンフルエンスボーナスを計算する"""
    
    confluence_count = 0
    confluence_zone = current_price * 0.001 
    
    for _, price in fib_levels.items():
        if abs(sl_price - price) <= confluence_zone:
            confluence_count += 1
            
    pivot_ratios = [0.995, 0.99] if side == 'ロング' else [1.005, 1.01] # ロングSLは下、ショートSLは上
    
    for ratio in pivot_ratios:
        pivot_price_proxy = current_price * ratio
        if abs(sl_price - pivot_price_proxy) <= confluence_zone:
             confluence_count += 1
             
    if confluence_count >= 2:
        return MULTI_PIVOT_CONFLUENCE_BONUS
        
    return 0.0

def check_vpvr_gap_penalty(df: pd.DataFrame, sl_price: float, side: str) -> bool:
    """エントリー価格とSLの間に出来高空隙（Gap）があるかをチェックする (簡易版)"""
    
    if side == 'ロング':
        # SLからエントリー価格までの間に出来高空隙があればペナルティ
        return df.iloc[-5:]['VPVR_Gap_Proxy'].any() # 簡易的に直近5本でチェック
    else: # ショート
        # エントリー価格からSLまでの間に出来高空隙があればペナルティ
        return df.iloc[-5:]['VPVR_Gap_Proxy'].any() 
            
    return False

def get_liquidity_bonus(symbol: str, price: float, side: str) -> float:
    """オーダーブックのデータに基づき流動性ボーナスを計算する (簡易版)"""
    orderbook = ORDER_BOOK_CACHE.get(symbol)
    if not orderbook or not orderbook.get('bids'): return 0.0
    
    # 簡易的に、オーダーブックの総量が十分あればボーナス
    total_volume = sum(item[1] for item in orderbook['bids']) + sum(item[1] for item in orderbook['asks'])
    
    if total_volume > 200: 
        return LIQUIDITY_BONUS_POINT
        
    return 0.0

def get_whale_bias_score(symbol: str, price: float, side: str) -> Tuple[float, float]:
    """🐋 オーダーブック内の大口注文の偏り（クジラバイアス）を計算する (簡易版)"""
    orderbook = ORDER_BOOK_CACHE.get(symbol)
    if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
        return 0.0, 0.0

    total_buy_volume_usdt = sum(p * a for p, a in orderbook['bids'])
    total_sell_volume_usdt = sum(p * a for p, a in orderbook['asks'])
    
    if total_buy_volume_usdt + total_sell_volume_usdt < 1_000_000:
         return 0.0, 0.0 

    imbalance_ratio = total_buy_volume_usdt / (total_buy_volume_usdt + total_sell_volume_usdt)

    score_impact = 0.0
    whale_imbalance_penalty_value = 0.0

    if side == 'ロング':
        if imbalance_ratio > WHALE_IMBALANCE_THRESHOLD:
            score_impact = WHALE_IMBALANCE_BONUS
        elif imbalance_ratio < 1.0 - WHALE_IMBALANCE_THRESHOLD:
            whale_imbalance_penalty_value = -WHALE_IMBALANCE_PENALTY
            score_impact = whale_imbalance_penalty_value
    else: # ショート
        if imbalance_ratio < 1.0 - WHALE_IMBALANCE_THRESHOLD:
            score_impact = WHALE_IMBALANCE_BONUS
        elif imbalance_ratio > WHALE_IMBALANCE_THRESHOLD:
            whale_imbalance_penalty_value = -WHALE_IMBALANCE_PENALTY
            score_impact = whale_imbalance_penalty_value

    return score_impact, whale_imbalance_penalty_value

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict, funding_oi_context: Dict, side: str) -> Optional[Dict]:
    """単一の時間足と銘柄に対して分析を実行する (v19.0.27 - Fee-Adjusted)"""

    if df is None or len(df) < 50: return None

    df_analyzed = calculate_indicators(df)
    last = df_analyzed.iloc[-1]
    prev = df_analyzed.iloc[-2]

    current_price = last['close']
    
    score = BASE_SCORE
    tech_data = {}

    # 1. 損切りと利確の設定 (Dual-Side)
    avg_range = last['AvgRange']
    range_sl_amount = avg_range * RANGE_TRAIL_MULTIPLIER
    risk_amount_usd_per_unit = range_sl_amount # 1ユニットあたりのSL幅（ドル）

    if side == 'ロング':
        sl_price = current_price - range_sl_amount
        tp1_price = current_price + (risk_amount_usd_per_unit * DTS_RRR_DISPLAY)
    else: # ショート
        sl_price = current_price + range_sl_amount
        tp1_price = current_price - (risk_amount_usd_per_unit * DTS_RRR_DISPLAY)

    # 2. 構造的SLの検討 (Fibレベル)
    low_price = df_analyzed['low'].min()
    high_price = df_analyzed['high'].max()
    fib_levels = {
        '0.236': get_fibonacci_level(high_price, low_price, 0.236),
        '0.382': get_fibonacci_level(high_price, low_price, 0.382),
        '0.500': get_fibonacci_level(high_price, low_price, 0.500),
    }

    closest_fib_level_price = min(fib_levels.values(), key=lambda x: abs(x - sl_price))
    structural_pivot_bonus = 0.0
    if abs(sl_price - closest_fib_level_price) / sl_price < 0.005: 
        structural_pivot_bonus = 0.05
    
    score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    tech_data['fib_proximity_level'] = [k for k, v in fib_levels.items() if v == closest_fib_level_price][0] if structural_pivot_bonus else 'N/A'

    # 多重ピボットコンフルエンスボーナス
    multi_pivot_confluence_bonus_value = get_multi_pivot_confluence(sl_price, fib_levels, current_price, side)
    score += multi_pivot_confluence_bonus_value
    tech_data['multi_pivot_confluence_bonus_value'] = multi_pivot_confluence_bonus_value

    # 3. RRRによるボーナス (グロスRRR)
    rr_ratio_gross = calculate_risk_reward_ratio(current_price, sl_price, tp1_price, side)
    rrr_bonus_value = 0.0
    if rr_ratio_gross >= MIN_RRR_THRESHOLD:
        rrr_bonus_value = RRR_BONUS_MULTIPLIER 
        score += rrr_bonus_value
        
    tech_data['rrr_bonus_value'] = rrr_bonus_value

    # --- スコアリング (Dual-Side) ---

    # 4. 長期トレンドフィルター (4h足の50SMA/EMA 200)
    long_term_reversal_penalty = False
    long_term_trend = 'Sideways'
    if timeframe == '4h':
        sma_50 = last['SMA_50']
        ema_200 = last['EMA_200']
        is_uptrend = current_price > sma_50 and current_price > ema_200
        is_downtrend = current_price < sma_50 and current_price < ema_200
        long_term_trend = 'Up' if is_uptrend else ('Down' if is_downtrend else 'Sideways')
        tech_data['long_term_trend'] = long_term_trend

        if (side == 'ロング' and long_term_trend == 'Down') or (side == 'ショート' and long_term_trend == 'Up'):
             score -= LONG_TERM_REVERSAL_PENALTY
             long_term_reversal_penalty = True
        elif (side == 'ロング' and long_term_trend == 'Up') or (side == 'ショート' and long_term_trend == 'Down'):
             score += LONG_TERM_REVERSAL_PENALTY # 逆行しない場合はボーナスとして加算
             
    tech_data['long_term_reversal_penalty'] = long_term_reversal_penalty

    # 5. モメンタム (RSI, MACD, Stoch)

    # RSI (Dual-Side)
    rsi_overbought_penalty = 0
    rsi_support_touch_bonus = 0.0
    
    if side == 'ロング':
        if last['RSI_14'] > RSI_MOMENTUM_LOW and prev['RSI_14'] <= RSI_MOMENTUM_LOW: # 40以下からの反発
             rsi_support_touch_bonus = RSI_SUPPORT_TOUCH_BONUS
             score += rsi_support_touch_bonus
        elif last['RSI_14'] > RSI_OVERBOUGHT_THRESHOLD: # 60以上でペナルティ
             score -= RSI_OVERBOUGHT_PENALTY
             rsi_overbought_penalty = 1
    else: # ショート
        if last['RSI_14'] < RSI_OVERBOUGHT_THRESHOLD and prev['RSI_14'] >= RSI_OVERBOUGHT_THRESHOLD: # 60以上からの反落
             rsi_support_touch_bonus = RSI_SUPPORT_TOUCH_BONUS
             score += rsi_support_touch_bonus
        elif last['RSI_14'] < RSI_MOMENTUM_LOW: # 40以下でペナルティ
             score -= RSI_OVERBOUGHT_PENALTY # RSIペナルティを流用
             rsi_overbought_penalty = 1
    
    tech_data['rsi_overbought_penalty'] = rsi_overbought_penalty
    tech_data['rsi_support_touch_bonus'] = rsi_support_touch_bonus
    
    # RSIダイバージェンスペナルティ (Dual-Side)
    rsi_divergence_penalty = check_rsi_divergence(df_analyzed, current_price, side)
    if rsi_divergence_penalty: score -= RSI_DIVERGENCE_PENALTY
    tech_data['rsi_divergence_penalty'] = rsi_divergence_penalty

    # MACDクロス確認 (Dual-Side)
    macd_cross_valid = False
    if side == 'ロング':
        macd_cross_valid = last['MACDh_12_26_9'] > 0 and prev['MACDh_12_26_9'] < 0 # ゴールデンクロス
    else:
        macd_cross_valid = last['MACDh_12_26_9'] < 0 and prev['MACDh_12_26_9'] > 0 # デッドクロス
        
    if macd_cross_valid:
         score += MACD_CROSS_BONUS
    # else: MACDデッドクロス/ゴールデンクロス不成立は個別にペナルティ計上

    tech_data['macd_cross_valid'] = macd_cross_valid

    # MACDダイバージェンスチェック (Dual-Side)
    macd_divergence_penalty = False
    if side == 'ロング':
         macd_divergence_penalty = last['MACDh_12_26_9'] < prev['MACDh_12_26_9'] and current_price > prev['close'] # 弱気ダイバージェンス
    else:
         macd_divergence_penalty = last['MACDh_12_26_9'] > prev['MACDh_12_26_9'] and current_price < prev['close'] # 強気ダイバージェンス
         
    if macd_divergence_penalty: score -= MACD_DIVERGENCE_PENALTY
    tech_data['macd_divergence_penalty'] = macd_divergence_penalty

    # Stochastics (Dual-Side)
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    stoch_filter_penalty = 0
    if side == 'ロング':
        if stoch_k < 20 and stoch_d < 20: score += 0.05 # 売られすぎからの反発期待
        elif stoch_k > 80 or stoch_d > 80: 
             score -= 0.15
             stoch_filter_penalty = 1
    else: # ショート
        if stoch_k > 80 and stoch_d > 80: score += 0.05 # 買われすぎからの反落期待
        elif stoch_k < 20 or stoch_d < 20: 
             score -= 0.15
             stoch_filter_penalty = 1
             
    tech_data['stoch_filter_penalty'] = stoch_filter_penalty

    # 6. ADX (トレンドの強さ) (Dual-Side)
    adx = last['ADX']
    di_plus = last['DMP']
    di_minus = last['DMN']
    tech_data['adx'] = adx
    tech_data['di_plus'] = di_plus
    tech_data['di_minus'] = di_minus
    adx_strength_bonus_value = 0.0
    adx_absence_penalty = 0.0

    is_strong_long_trend = adx > 25 and di_plus > di_minus
    is_strong_short_trend = adx > 25 and di_minus > di_plus

    if (side == 'ロング' and is_strong_long_trend) or (side == 'ショート' and is_strong_short_trend):
        adx_strength_bonus_value = ADX_TREND_STRENGTH_BONUS
        score += adx_strength_bonus_value
    
    elif adx < ADX_ABSENCE_THRESHOLD:
        adx_absence_penalty = -ADX_TREND_ABSENCE_PENALTY
        score += adx_absence_penalty
        
    tech_data['adx_strength_bonus_value'] = adx_strength_bonus_value
    tech_data['adx_absence_penalty'] = adx_absence_penalty

    # 7. 出来高確認 (ボリュームは両側共通)
    volume_confirmation_bonus = 0.0
    if last['volume'] > last['Volume_Avg_50'] * VOLUME_CONFIRMATION_MULTIPLIER:
        volume_confirmation_bonus = 0.05
        score += volume_confirmation_bonus
    tech_data['volume_confirmation_bonus'] = volume_confirmation_bonus

    # 8. 市場構造: VPVR出来高空隙ペナルティ (Dual-Side)
    vpvr_gap_penalty_flag = check_vpvr_gap_penalty(df_analyzed, sl_price, side)
    vpvr_gap_penalty_value = 0.0
    if vpvr_gap_penalty_flag:
        vpvr_gap_penalty_value = -VPVR_GAP_PENALTY
        score += vpvr_gap_penalty_value
        
    tech_data['vpvr_gap_penalty'] = vpvr_gap_penalty_value

    # 9. 流動性ボーナス & クジラバイアス (Dual-Side)
    liquidity_bonus_value = get_liquidity_bonus(symbol, current_price, side)
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    whale_score_impact, whale_imbalance_penalty_value = get_whale_bias_score(symbol, current_price, side)
    score += whale_score_impact
    tech_data['whale_imbalance_bonus'] = whale_score_impact if whale_score_impact > 0 else 0.0
    tech_data['whale_imbalance_penalty'] = whale_imbalance_penalty_value if whale_imbalance_penalty_value < 0 else 0.0

    # 10. ボラティリティ（BBands）と価格構造 (VWAP) (Dual-Side)
    bb_width_percent = (last['BBU_5_2.0'] - last['BBL_5_2.0']) / last['close'] * 100
    bb_squeeze_ok = bb_width_percent < 5.0
    bb_wide_penalty_flag = bb_width_percent > 8.0
    
    if bb_squeeze_ok: score += BB_SQUEEZE_BONUS
    if bb_wide_penalty_flag: score -= BB_WIDE_PENALTY

    tech_data['bb_squeeze_ok'] = bb_squeeze_ok
    tech_data['bb_wide_penalty'] = bb_wide_penalty_flag

    if side == 'ロング':
        vwap_confirm_ok = last['close'] > last['VWAP']
    else:
        vwap_confirm_ok = last['close'] < last['VWAP']
        
    if vwap_confirm_ok: score += VWAP_BONUS_POINT
    tech_data['vwap_confirm_ok'] = vwap_confirm_ok

    # 11. マクロセンチメント (FGI + FX要因の統合)
    sentiment_fgi_proxy_value = macro_context.get('sentiment_fgi_proxy_value', 0.0)
    score += sentiment_fgi_proxy_value * (1 if side == 'ロング' else -1) # ロングはリスクオンでボーナス、ショートはリスクオフでボーナス

    dxy_surge_penalty = macro_context.get('dxy_surge_penalty', 0.0)
    usdjpy_risk_off_penalty = macro_context.get('usdjpy_risk_off_penalty', 0.0)
    fx_macro_stability_bonus = macro_context.get('usdjpy_stability_bonus', 0.0) 

    # ロングの場合: DXY/USDJPYネガティブはペナルティ。安定はボーナス
    # ショートの場合はリスクオフ要因をボーナスとして評価するためにペナルティを逆転
    if side == 'ロング':
        score += dxy_surge_penalty
        score += usdjpy_risk_off_penalty
        score += fx_macro_stability_bonus
    else: # ショート
        score -= dxy_surge_penalty
        score -= usdjpy_risk_off_penalty
        # FX_MACRO_STABILITY_BONUSはショートでは適用しない (中立)
        
    tech_data['dxy_surge_penalty'] = dxy_surge_penalty
    tech_data['usdjpy_risk_off_penalty'] = usdjpy_risk_off_penalty
    tech_data['fx_macro_stability_bonus'] = fx_macro_stability_bonus
    
    # 12. Funding Rate / Open Interest要因の統合 (Dual-Side)
    oi_surge_penalty = funding_oi_context.get('oi_surge_penalty', 0.0)
    fr_high_penalty = funding_oi_context.get('fr_high_penalty', 0.0)
    fr_low_bonus = funding_oi_context.get('fr_low_bonus', 0.0)
    
    score += oi_surge_penalty # OI急増は両方にとってペナルティ
    
    if side == 'ロング':
        score += fr_high_penalty # FRポジティブはロングにとってペナルティ
        score += fr_low_bonus   # FRネガティブはロングにとってボーナス
    else: # ショート
        score -= fr_high_penalty # FRポジティブはショートにとってボーナス (ペナルティを逆転)
        score -= fr_low_bonus   # FRネガティブはショートにとってペナルティ (ボーナスを逆転)
    
    tech_data['oi_surge_penalty'] = oi_surge_penalty
    tech_data['fr_high_penalty'] = fr_high_penalty
    tech_data['fr_low_bonus'] = fr_low_bonus
    
    final_score = min(1.00, score)

    # 💡 v19.0.27 修正箇所: リスク許容額の動的決定 (スコアとRRRに連動)
    
    # 1. ダイナミック乗数 (0.0 ～ 1.0) の計算:
    score_factor = max(0.0, (final_score - TRADE_EXECUTION_THRESHOLD) / (1.0 - TRADE_EXECUTION_THRESHOLD)) # 0.85で0, 1.0で1
    rrr_factor = max(0.0, (rr_ratio_gross - MIN_RRR_THRESHOLD) / (6.0 - MIN_RRR_THRESHOLD)) # 3.0で0, 6.0で1
    
    # RRRを重視するため、RRRの重みを大きくする (例: 70% RRR, 30% Score)
    dynamic_multiplier = max(0.0, min(1.0, rrr_factor * 0.7 + score_factor * 0.3)) 

    # 2. 最終的な残高リスク率の決定 (MAX_BASE_RISK_CAPITAL_PERCENT ～ MAX_DYNAMIC_RISK_CAPITAL_PERCENT)
    final_risk_pct = MAX_BASE_RISK_CAPITAL_PERCENT + (MAX_DYNAMIC_RISK_CAPITAL_PERCENT - MAX_BASE_RISK_CAPITAL_PERCENT) * dynamic_multiplier
    
    # 3. 最終リスク許容額の計算 (残高のリスク率と絶対額上限の小さい方を適用)
    risk_from_balance = CURRENT_USDT_BALANCE * final_risk_pct
    risk_amount_usdt = round(min(risk_from_balance, MAX_ABS_RISK_USDT_CAP), 2)
    
    # 4. ポジション数量の計算 (手数料を考慮)
    # R = Q * (r_u + E * 2f) より -> Q = R / (r_u + E * 2f)
    
    # 単位あたりのリスク合計額 (損切り幅 + 往復手数料)
    risk_per_unit_total = range_sl_amount + (current_price * TRADING_FEE_RATE_ROUND_TRIP)
    
    # 手数料を考慮したポジション数量
    quantity_units = 0.0
    if risk_per_unit_total > 0 and risk_amount_usdt > 0.01:
        quantity_units = risk_amount_usdt / risk_per_unit_total
    
    # 5. 手数料とリワードの計算
    position_value_usdt = quantity_units * current_price
    estimated_fee_usdt = position_value_usdt * TRADING_FEE_RATE_ROUND_TRIP # 往復手数料 (SL/TP時の手数料を含む)
    
    # グロスリワード額
    reward_per_unit = range_sl_amount * rr_ratio_gross
    gross_reward_usdt = quantity_units * reward_per_unit
    
    # 実質リワード額 = グロスリワード - 往復手数料
    net_reward_amount = gross_reward_usdt - estimated_fee_usdt
    
    # 実質RRR = (実質リワード / リスク許容額)
    net_rr_ratio = round(net_reward_amount / risk_amount_usdt, 2) if risk_amount_usdt > 0 else 0.0

    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'score_raw': score,
        'entry_price': current_price,
        'rr_ratio_gross': rr_ratio_gross, 
        'risk_amount_usdt': risk_amount_usdt,
        'dynamic_risk_pct': final_risk_pct, 
        'trade_plan': {'sl_price': sl_price, 'tp1_price': tp1_price, 'risk_amount_usd_per_unit': risk_amount_usd_per_unit},
        'tech_data': tech_data,
        'quantity_units': quantity_units, 
        'position_value_usdt': position_value_usdt,
        'estimated_fee_usdt': estimated_fee_usdt,
        'net_reward_amount': net_reward_amount,
        'net_rr_ratio': net_rr_ratio,
    }

# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

async def execute_trade(signal: Dict):
    """シグナルに基づきポジションを取得する (シミュレーション: 現物ロングのみ)"""
    symbol = signal['symbol']
    side = signal['side']
    
    # 💡 実際の取引所API連携ロジック（現物買い注文）は省略
    # order = await EXCHANGE_CLIENT.create_order(...)

    # ポジションのステータス更新 (シミュレーション)
    ACTUAL_POSITIONS[symbol] = {
        'entry': signal['entry_price'], 'sl': signal['trade_plan']['sl_price'],
        'tp': signal['trade_plan']['tp1_price'], 'size': signal['quantity_units'], 
        'status': 'open', 'timestamp': time.time(), 'side': side
    }
    
    dynamic_risk_pct = signal.get('dynamic_risk_pct', MAX_BASE_RISK_CAPITAL_PERCENT) * 100
    net_rr_ratio = signal.get('net_rr_ratio', 0.0)
    position_value_usdt = signal.get('position_value_usdt', 0.0)

    notification_msg = (
        f"🤖 **自動取引実行通知**\n"
        f"銘柄: **{symbol}** | 方向: **{side}**\n"
        f"スコア: {signal['score'] * 100:.2f} | 実質RRR: 1:{net_rr_ratio:.2f} ✨\n"
        f"リスク率: {dynamic_risk_pct:.2f}% ({format_usdt(signal['risk_amount_usdt'])} USDT)\n"
        f"ポジション総額: ${format_usdt(position_value_usdt)}\n"
        f"エントリー: ${format_price_utility(signal['entry_price'])}"
    )
    send_position_status_notification(notification_msg)


async def main_loop():
    """BOTのメイン処理ループ (ショートシグナルの取引実行をスキップ)"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CURRENT_USDT_BALANCE, CURRENT_BALANCE_STATUS, FX_MACRO_CONTEXT, FUNDING_OI_CONTEXT
    
    while True:
        try:
            logging.info("--- メインループ開始 ---")
            
            # 💡 修正点: ロバストな残高取得とステータス更新
            CURRENT_USDT_BALANCE, CURRENT_BALANCE_STATUS = await fetch_current_balance_usdt_with_status(EXCHANGE_CLIENT)
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, USDT残高: {format_usdt(CURRENT_USDT_BALANCE)}, ステータス: {CURRENT_BALANCE_STATUS})")
            
            await update_symbols_by_volume(EXCHANGE_CLIENT)
            
            # 💡 修正点: マクロコンテキスト取得 (エラーハンドリング強化済み)
            FX_MACRO_CONTEXT = await get_fx_macro_context()
            
            all_signals: List[Dict] = []
            
            # 4. 全銘柄の分析とシグナル生成
            for symbol in CURRENT_MONITOR_SYMBOLS:
                await fetch_order_book_depth(symbol) 
                FUNDING_OI_CONTEXT = await get_funding_oi_context(symbol)
                
                for timeframe in ['1h', '4h']:
                    df_ohlcv, status, client = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe)
                    if status == "Success" and df_ohlcv is not None:
                        # ロングシグナルの分析
                        analysis_long = analyze_single_timeframe(df_ohlcv, timeframe, symbol, FX_MACRO_CONTEXT, FUNDING_OI_CONTEXT, 'ロング')
                        if analysis_long: all_signals.append(analysis_long)
                        
                        # ショートシグナルの分析
                        analysis_short = analyze_single_timeframe(df_ohlcv, timeframe, symbol, FX_MACRO_CONTEXT, FUNDING_OI_CONTEXT, 'ショート')
                        if analysis_short: all_signals.append(analysis_short)

                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)
            
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # 5. 実行閾値(0.85)以上のシグナルを抽出 (即時通知対象)
            tradable_signals = sorted([s for s in all_signals if s['score'] >= TRADE_EXECUTION_THRESHOLD], 
                                      key=lambda x: x.get('score', 0.0) * 0.7 + x.get('net_rr_ratio', 0.0) * 0.3, 
                                      reverse=True)
            
            # 6. 実行閾値(0.85)以上のシグナルの即時通知と取引実行
            for signal in tradable_signals:
                symbol_side = f"{signal['symbol']}-{signal['side']}"
                side = signal['side']

                # クールダウンチェック (実行したシグナルは通知を繰り返さない)
                if time.time() - TRADE_COOLDOWN_TIMESTAMPS.get(symbol_side, 0) < TRADE_SIGNAL_COOLDOWN: continue
                
                # 詳細な分析通知
                message = format_integrated_analysis_message(signal['symbol'], [signal], 1)
                if message:
                    send_telegram_html(message)
                    TRADE_COOLDOWN_TIMESTAMPS[symbol_side] = time.time()
                
                # 取引実行 (ロングのみ実行, 残高チェック)
                if side == 'ロング' and CURRENT_BALANCE_STATUS == "TRADABLE" and signal['risk_amount_usdt'] > 0:
                    await execute_trade(signal)
                elif side == 'ロング':
                    logging.warning(f"{symbol_side}: 残高ステータス ({CURRENT_BALANCE_STATUS}) 不足のため取引をスキップしました。")
                else:
                    logging.info(f"{symbol_side}: ショートシグナルのため、現物取引実行はスキップしました。（通知のみ実行）")

            # 7. 0.70以上のシグナルをログに記録 (即時通知対象外のシグナル)
            high_score_signals_log = sorted([s for s in all_signals if s.get('score', 0.0) >= SIGNAL_THRESHOLD and s.get('score', 0.0) < TRADE_EXECUTION_THRESHOLD], key=lambda x: x.get('score', 0.0), reverse=True)
            for signal in high_score_signals_log[:TOP_SIGNAL_COUNT]:
                logging.info(f"スコア {signal['score']:.2f} (未実行): {signal['symbol']} - {signal['side']} - {signal['timeframe']}")

            LAST_SUCCESS_TIME = time.time()
            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.27 - Final Integrated Build)。次回の実行まで {LOOP_INTERVAL} 秒待機。")

        except Exception as e:
            logging.error(f"メインループで致命的なエラーが発生: {e}", exc_info=True)
            await asyncio.sleep(LOOP_INTERVAL * 2)

        await asyncio.sleep(LOOP_INTERVAL)

# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.27 - Final Integrated Build")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.27 Startup initializing (Final Integrated Build)...")

    await initialize_ccxt_client()

    # 💡 初回通知で残高ステータスも取得し、メッセージに含める
    usdt_balance, status = await fetch_current_balance_usdt_with_status(EXCHANGE_CLIENT)
    await send_position_status_notification(f"🤖 BOT v19.0.27 初回起動通知 | 残高ステータス: {status}")

    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.27 - Final Integrated Build",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions": len(ACTUAL_POSITIONS),
        "current_usdt_balance": CURRENT_USDT_BALANCE,
        "balance_status": CURRENT_BALANCE_STATUS
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.27 - Final Integrated Build"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
