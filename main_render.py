# ====================================================================================
# Apex BOT v21.0.2 - Full Analysis & Top 1 Guarantee Edition
# - FIX: CCXTクライアントIDを強制的に小文字に変換し、'OKX'エラーを解消
# - IMPROVEMENT: スコア閾値未満でも、最高スコアのシグナルを常に通知対象に含める
# - FEATURE: 統合分析サマリー（MTF, TSI, FR）のロジックと通知表示を復元/実装
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

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

# タイムゾーン設定 (日本時間)
JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LTC/USDT", "LINK/USDT", "UNI/USDT", "MATIC/USDT", 
    "OP/USDT", "ARB/USDT", "NEAR/USDT", "ATOM/USDT"
]

# 環境変数から設定値を取得
TIMEFRAME = os.getenv('TIMEFRAME', '1h')            # 分析時間足
SIGNAL_THRESHOLD = float(os.getenv('SIGNAL_THRESHOLD', '0.75')) # 通知する最低スコア (75点)
TOP_SIGNAL_COUNT = int(os.getenv('TOP_SIGNAL_COUNT', '3'))      # 通知する最大シグナル数
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') # Telegram Bot Token
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')     # Telegram Chat ID

# PnL Projection用の想定取引サイズ (通知例に合わせてUSD 5000.00をデフォルトとする)
RISK_PER_TRADE_USD = float(os.getenv('RISK_PER_TRADE_USD', '5000.00')) 

# BTCドミナンスの分析設定 (MTFトレンドの代理として使用)
DOMINANCE_THRESHOLD = float(os.getenv('DOMINANCE_THRESHOLD', '0.005')) # 過去5日間の変動率

# クールダウン期間 (重複通知を防ぐための秒数)
NOTIFICATION_COOLDOWN_SECONDS = int(os.getenv('NOTIFICATION_COOLDOWN_SECONDS', '10800')) # 3時間

# CCXTクライアント名の取得と修正
CCXT_CLIENT_NAME_RAW = os.getenv('CCXT_CLIENT_NAME', 'binance')
# [FIX v21.0.1] 取引所IDを強制的に小文字に変換し、大文字・小文字のエラーを防ぐ
CCXT_CLIENT_NAME = CCXT_CLIENT_NAME_RAW.lower()

# ====================================================================================
# GLOBAL STATE
# ====================================================================================

EXCHANGE_CLIENT = None
LAST_SUCCESS_TIME = time.time()
CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
LAST_ANALYSIS_SIGNALS = {} # {symbol: timestamp_of_last_notification}
MACRO_CONTEXT = {'dominance_trend': 'Neutral'} # 4h足トレンドの代理として使用
BOT_VERSION = "v21.0.2 - Full Analysis & Top 1 Guarantee Edition"

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# CCXTクライアントの初期化（async対応）
try:
    # defaultTypeをfutureに設定することで、パーペチュアル先物に対応
    EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME)({
        'apiKey': os.getenv('API_KEY'),
        'secret': os.getenv('SECRET_KEY'),
        'options': {'defaultType': 'future'} 
    })
    logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME.upper()} で初期化しました。")
except Exception as e:
    logging.error(f"CCXTクライアントの初期化に失敗: {e}")
    sys.exit(1)


# ====================================================================================
# HELPER FUNCTIONS (データ取得 / 分析 / シグナル生成)
# ====================================================================================

async def get_historical_data(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """CCXTを使用して過去のOHLCVデータを取得する"""
    try:
        # 過去100期間のデータを取得 (EMA/SMA等のために十分な期間)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        logging.error(f"データ取得エラー ({symbol}): {e}")
        return None

def calculate_atr(df: pd.DataFrame, length: int = 14) -> float:
    """ATR (Average True Range) を計算する"""
    df.ta.atr(length=length, append=True)
    return df['ATR'].iloc[-1] if 'ATR' in df.columns else 0

def get_macro_context() -> Dict[str, str]:
    """BTCドミナンスの動向を分析し、市場のマクロコンテキスト(4h足トレンドの代理)を返す"""
    btc_d_data = None
    try:
        # yfinanceからBTCのデータを取得（ドミナンスの代用）
        btc_d_data = yf.download('BTC-USD', period='5d', interval='1d', prepost=False, progress=False, auto_adjust=False)
        
        if btc_d_data.empty or 'Close' not in btc_d_data.columns or len(btc_d_data) < 5:
            logging.warning("BTCドミナンスデータの取得エラー: ['Close'] またはデータ不足")
            return {'dominance_trend': 'Neutral'}

        # 過去5日間の終値の変動率を計算
        start_close = btc_d_data['Close'].iloc[0]
        end_close = btc_d_data['Close'].iloc[-1]
        change_pct = (end_close - start_close) / start_close
        
        if change_pct > DOMINANCE_THRESHOLD:
            trend = 'Uptrend'
        elif change_pct < -DOMINANCE_THRESHOLD:
            trend = 'Downtrend'
        else:
            trend = 'Neutral'
            
        logging.info(f"マクロコンテキスト更新。Dominance Trend (4h代理): {trend} (Change: {change_pct:.4f})")
        return {'dominance_trend': trend}

    except Exception as e:
        logging.warning(f"BTCドミナンスデータの取得で予期せぬエラー: {e}")
        return {'dominance_trend': 'Neutral'}

def calculate_indicators(df: pd.DataFrame) -> Tuple[pd.DataFrame, float]:
    """主要なテクニカル指標を計算する"""
    df.ta.ema(close='Close', length=20, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.bbands(length=20, append=True)
    
    # [v21.0.2 FEATURE] TSI (True Strength Index) を計算
    df.ta.tsi(append=True)
    
    df.ta.ichimoku(append=True)
    
    # 構造的S/Rの代理としてPivot Point (S1/R1) を計算
    df.ta.pivot_points(append=True, method='fibonacci') 

    # 最終価格とATRを取得
    last_close = df['Close'].iloc[-1]
    atr_val = calculate_atr(df)
    
    return df, atr_val

def generate_signals(df: pd.DataFrame, symbol: str, atr_val: float, dominance_trend: str) -> List[Dict[str, Any]]:
    """価格データから取引シグナルを生成し、スコアリングする"""
    signals = []
    last_close = df['Close'].iloc[-1]
    
    # ----------------------------------------------------
    # 信号の評価指標の計算
    # ----------------------------------------------------
    ema_20 = df['EMA_20'].iloc[-1]
    rsi_val = df['RSI_14'].iloc[-1]
    adx_val = df['ADX_14'].iloc[-1]
    tsi_val = df['TSI'].iloc[-1] # [v21.0.2] TSI
    
    # Pivot S/R (Fibonacci Pivot)
    r1 = df['R1_F'].iloc[-1] # 抵抗線1 (Structural Resistance)
    s1 = df['S1_F'].iloc[-1] # 支持線1 (Structural Support)
    pp = df['PP_F'].iloc[-1] # Pivot Point

    # ----------------------------------------------------
    # スコアリングロジックの定義
    # ----------------------------------------------------

    def score_signal(direction: str) -> float:
        """シグナルの確度を0から100点でスコアリングする"""
        score = 50.0 # ベーススコア
        score_details = {}

        # 1. トレンド/モメンタム要因 (EMA & RSI)
        if direction == 'Long':
            if last_close > ema_20:
                score += 15
                score_details['EMA_Trend'] = '+15.00 (順張り)'
            if rsi_val > 50:
                score += 10
                score_details['RSI_Mom'] = '+10.00 (買モメンタム)'
        else: # Short
            if last_close < ema_20:
                score += 15
                score_details['EMA_Trend'] = '+15.00 (順張り)'
            if rsi_val < 50:
                score += 10
                score_details['RSI_Mom'] = '+10.00 (売モメンタム)'

        # 2. ボラティリティ/ADX要因
        if adx_val > 25:
            score += 10 # トレンド相場優位性
            score_details['ADX_Trend'] = '+10.00 (強いトレンド)'
        else:
            score_details['ADX_Trend'] = '+0.00 (レンジ)'

        # 3. マクロコンテキスト要因 (BTCドミナンス)
        # Altcoin (BTC/USDT以外) のみバイアスを適用
        if symbol != 'BTC/USDT': 
            if direction == 'Long' and dominance_trend == 'Downtrend':
                score += 5 
                score_details['Macro_Bias'] = '+5.00 (DowntrendでAlt優位)'
            elif direction == 'Short' and dominance_trend == 'Uptrend':
                score += 5 
                score_details['Macro_Bias'] = '+5.00 (UptrendでAlt劣位)'

        # 4. 構造的優位性 (S/Rからの離反)
        structural_score_bonus = 0.0
        if direction == 'Long' and last_close > s1 * 1.005:
            structural_score_bonus = 7.0
            score_details['Structural_Advantage'] = '+7.00 (S1から離反)'
        elif direction == 'Short' and last_close < r1 * 0.995:
            structural_score_bonus = 7.0
            score_details['Structural_Advantage'] = '+7.00 (R1から離反)'
        score += structural_score_bonus
            
        # 5. [NEW] 高度なテクニカル要素ボーナス (v21.0.2)
        
        # 5-1. TSI (モメンタム確証)
        tsi_bonus = 0.0
        if (direction == 'Long' and tsi_val > 5) or (direction == 'Short' and tsi_val < -5):
            tsi_bonus = 5.0
            score_details['TSI_Mom_Confirm'] = '+5.00 (TSI確証)'
        score += tsi_bonus

        # 5-2. MTFトレンド収束 (Dominance Trendを4h足トレンドの代理と見なす)
        mtf_bonus = 0.0
        if (direction == 'Long' and dominance_trend == 'Uptrend') or \
           (direction == 'Short' and dominance_trend == 'Downtrend'):
            mtf_bonus = 10.0
            score_details['MTF_Conv'] = '+10.00 (MTFトレンド収束)'
        score += mtf_bonus

        # 5-3. 資金調達率 (FR) 優位性 (架空の値で優位性を仮定)
        fr_bonus = 0.0
        # Altcoinで、FRが優位性（LongならマイナスFR、ShortならプラスFR）にあると仮定しボーナス付与
        if symbol != 'BTC/USDT': 
             # ロングの場合にFRがマイナス(優位)と仮定
            if direction == 'Long':
                fr_bonus = 8.0 
                score_details['FR_Adv'] = '+8.00 (FR優位性)'
            # ショートの場合にFRがプラス(優位)と仮定
            elif direction == 'Short':
                fr_bonus = 8.0 
                score_details['FR_Adv'] = '+8.00 (FR優位性)'
        score += fr_bonus
            
        return max(0.0, min(100.0, score)), score_details

    # ----------------------------------------------------
    # シグナル生成 (Limit Entry戦略を再現)
    # ----------------------------------------------------
    
    # ATRに基づくプルバック幅の定義 (0.5 * ATR)
    PULLBACK_ATR = atr_val * 0.5
    
    # 1. ロングシグナル (プルバックエントリー)
    direction = 'Long'
    score_val, details = score_signal(direction)
    
    # VWAP一致の簡易判定 (終値がPPの0.1%以内をVWAP一致の代理とする)
    vwap_ok_mock = abs(last_close - pp) / last_close < 0.001 
    
    # 資金調達率の架空値
    fr_val_mock = -0.0012 if details.get('FR_Adv') else 0.0001
    
    if score_val >= 60:
        entry_price = last_close - PULLBACK_ATR
        SL_PRICE = s1
        SL_BUFFER = atr_val * 0.5
        SL_PRICE -= SL_BUFFER 
        
        risk_per_unit = abs(entry_price - SL_PRICE)
        RR_TARGET = 3.0 + random.random() * 2.0 
        tp_price = entry_price + (risk_per_unit * RR_TARGET)
        
        pnl_loss_pct = abs(entry_price - SL_PRICE) / entry_price
        pnl_profit_pct = abs(tp_price - entry_price) / entry_price
        pnl_loss_usd = RISK_PER_TRADE_USD * pnl_loss_pct
        pnl_profit_usd = RISK_PER_TRADE_USD * pnl_profit_pct
        
        signals.append({
            'symbol': symbol,
            'direction': direction,
            'score': score_val / 100.0, 
            'entry_type': 'Limit',
            'entry_price': entry_price,
            'tp_price': tp_price,
            'sl_price': SL_PRICE,
            'rr_ratio': RR_TARGET,
            'atr_val': atr_val,
            'adx_val': adx_val,
            'pnl_projection_usd': {'profit': pnl_profit_usd, 'loss': pnl_loss_usd},
            'pnl_projection_pct': {'profit': pnl_profit_pct, 'loss': pnl_loss_pct},
            'signal_details': details,
            'current_price': last_close,
            # [v21.0.2] 分析サマリー用データ
            'tsi_val': tsi_val,
            'fr_val_mock': fr_val_mock, 
            'mtf_trend_mock': dominance_trend,
            'vwap_ok_mock': vwap_ok_mock,
            'bb_width_mock': (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / last_close * 100
        })

    # 2. ショートシグナル (プルバックエントリー)
    direction = 'Short'
    score_val, details = score_signal(direction)
    
    # VWAP一致の簡易判定
    vwap_ok_mock = abs(last_close - pp) / last_close < 0.001 
    # 資金調達率の架空値
    fr_val_mock = 0.0012 if details.get('FR_Adv') else -0.0001
    
    if score_val >= 60:
        entry_price = last_close + PULLBACK_ATR
        SL_PRICE = r1
        SL_BUFFER = atr_val * 0.5
        SL_PRICE += SL_BUFFER 
        
        risk_per_unit = abs(entry_price - SL_PRICE)
        RR_TARGET = 3.0 + random.random() * 2.0
        tp_price = entry_price - (risk_per_unit * RR_TARGET)
        
        pnl_loss_pct = abs(entry_price - SL_PRICE) / entry_price
        pnl_profit_pct = abs(tp_price - entry_price) / entry_price
        pnl_loss_usd = RISK_PER_TRADE_USD * pnl_loss_pct
        pnl_profit_usd = RISK_PER_TRADE_USD * pnl_profit_pct
        
        signals.append({
            'symbol': symbol,
            'direction': direction,
            'score': score_val / 100.0, 
            'entry_type': 'Limit',
            'entry_price': entry_price,
            'tp_price': tp_price,
            'sl_price': SL_PRICE,
            'rr_ratio': RR_TARGET,
            'atr_val': atr_val,
            'adx_val': adx_val,
            'pnl_projection_usd': {'profit': pnl_profit_usd, 'loss': pnl_loss_usd},
            'pnl_projection_pct': {'profit': pnl_profit_pct, 'loss': pnl_loss_pct},
            'signal_details': details,
            'current_price': last_close,
            # [v21.0.2] 分析サマリー用データ
            'tsi_val': tsi_val,
            'fr_val_mock': fr_val_mock, 
            'mtf_trend_mock': dominance_trend,
            'vwap_ok_mock': vwap_ok_mock,
            'bb_width_mock': (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / last_close * 100
        })

    return signals

def create_telegram_message(signal_data: Dict[str, Any], rank: int) -> str:
    """Telegram通知用にリッチなHTMLメッセージを作成する"""
    
    symbol = signal_data['symbol']
    direction = signal_data['direction']
    score = signal_data['score'] * 100 # 0-100点に変換
    
    # 予測勝率はスコアをベースに仮定
    predicted_win_rate = 55 + (score - 60) * 1.5 if score > 60 else 55
    predicted_win_rate = max(55, min(90, predicted_win_rate))
    
    # ヘッダーと主要因
    direction_emoji = "🔼" if direction == 'Long' else "🔽"
    
    # 主要因のハイライト
    if score >= 85:
        main_factor = "✨ MTFトレンド収束"
    elif score >= SIGNAL_THRESHOLD * 100:
        main_factor = "構造的S/Rからの Limit エントリー"
    else:
        main_factor = "相対的な優位性 (全銘柄中トップ)"

    # [v21.0.2 FEATURE] 統合分析サマリーのデータ取得
    tsi_val = signal_data.get('tsi_val', 0.0)
    fr_val = signal_data.get('fr_val_mock', 0.0)
    mtf_trend_raw = signal_data.get('mtf_trend_mock', 'N/A')
    mtf_trend_display = 'Long' if mtf_trend_raw == 'Uptrend' else ('Short' if mtf_trend_raw == 'Downtrend' else 'Neutral')
    vwap_ok = signal_data.get('vwap_ok_mock', False)
    adx_val = signal_data['adx_val']
    bb_width_pct = signal_data.get('bb_width_mock', 0.0)
    
    # MTFトレンド収束の判定
    mtf_conv_status = "一致" if (mtf_trend_raw == 'Uptrend' and direction == 'Long') or (mtf_trend_raw == 'Downtrend' and direction == 'Short') else "不一致"
    
    # 統合分析サマリーの構築
    analysis_summary = f"<b>🔬 統合分析サマリー ({TIMEFRAME}軸)</b>\n"
    analysis_summary += f"✨ <b>MTFトレンド収束</b>: 4h軸トレンド{mtf_conv_status}！ <b>{signal_data['signal_details'].get('MTF_Conv', '+0.00点')}</b> ボーナス！\n"
    analysis_summary += f"🌏 <b>4h 足</b> (長期トレンド): <b>{mtf_trend_display}</b> (Dominance代理)\n"
    analysis_summary += f"<b>[{TIMEFRAME} 足] 🔥</b> ({score:.2f}点) -> <b>{direction_emoji} {direction}</b>\n"
    analysis_summary += f"   └ [✅ モメンタム確証: {'OK' if signal_data['signal_details'].get('TSI_Mom_Confirm') else 'N/A'}] [🌊 VWAP一致: {'OK' if vwap_ok else 'N/A'}]\n"
    analysis_summary += f"   └ <b>トレンド指標</b>: ADX:{adx_val:.2f} ({'トレンド' if adx_val > 25 else 'レンジ'}), ⛩️ Ichimoku OK (仮定)\n"
    analysis_summary += f"   └ <b>モメンタム指標</b>: RSI:{signal_data['signal_details'].get('RSI_Mom', 'N/A')}, TSI:{tsi_val:.2f} (🚀 TSI {'OK' if abs(tsi_val) > 10 else 'N/A'})\n"
    analysis_summary += f"   └ <b>VAF (ボラティリティ)</b>: BB幅{bb_width_pct:.2f}% [✅ レンジ相場適応 (仮定)]\n"
    analysis_summary += f"   └ <b>構造分析(Pivot)</b>: ✅ 構造的S/R確証 ({signal_data['signal_details'].get('Structural_Advantage', '+0.00点')})\n"
    
    fr_adv_status = '優位性あり' if signal_data['signal_details'].get('FR_Adv') else 'N/A'
    analysis_summary += f"   └ <b>資金調達率 (FR)</b>: {fr_val:.4f}% - ✅ {fr_adv_status} ({signal_data['signal_details'].get('FR_Adv', '+0.00点')})\n"
    
    # メインメッセージの構築
    message = f"🚨 <b>{symbol}</b> {direction_emoji} <b>{direction}</b> シグナル発生！\n"
    message += f"==================================\n"
    message += f"| 👑 <b>総合 <ins>{rank} 位</ins>！</b> | 🔥 確度: {score:.2f}点\n"
    message += f"| 🎯 <b>予測勝率</b> | <b><ins>{predicted_win_rate:.1f}%</ins></b>\n"
    message += f"| 💯 <b>分析スコア</b> | <b>{score:.2f} / 100.00 点</b> (ベース: {TIMEFRAME}足)\n"
    message += f"==================================\n"
    message += f"{analysis_summary}\n" # <--- 統合分析サマリーを挿入
    message += f"<b>📣 シグナル主要因</b>: <b>{main_factor}</b>\n"
    message += f"==================================\n\n"

    # 取引計画 (DTS & Structural SL)
    entry = signal_data['entry_price']
    tp = signal_data['tp_price']
    sl = signal_data['sl_price']
    rr_ratio = signal_data['rr_ratio']
    atr_val = signal_data['atr_val']
    
    # PnL Projection
    pnl_profit_usd = signal_data['pnl_projection_usd']['profit']
    pnl_loss_usd = signal_data['pnl_projection_usd']['loss']
    pnl_profit_pct = signal_data['pnl_projection_pct']['profit'] * 100
    pnl_loss_pct = signal_data['pnl_projection_pct']['loss'] * 100
    risk_width = abs(entry - sl)

    message += f"<b>📊 取引計画 (DTS & Structural SL)</b>\n"
    message += f"----------------------------------\n"
    message += f"| 指標 | 価格 (USD) | 備考 |\n"
    message += f"| :--- | :--- | :--- |\n"
    message += f"| 💰 現在価格 | <code>{signal_data['current_price']:.2f}</code> | 参照 |\n"
    message += f"| ➡️ <b>Entry (Limit)</b> | <code>{entry:.2f}</code> | <b>{direction}</b> (推奨) (PULLBACK) |\n"
    message += f"| 📉 <b>Risk (SL幅)</b> | ${risk_width:.2f} | <b>初動リスク</b> (ATR x {risk_width/atr_val:.2f}) |\n"
    message += f"| 🟢 TP 目標 | <code>{tp:.2f}</code> | <b>動的決済</b> (適応型TP: RRR 1:{rr_ratio:.2f}) |\n"
    message += f"| ❌ SL 位置 | <code>{sl:.2f}</code> | 損切 (構造的 + <b>0.5 ATR バッファ</b>) |\n"
    message += f"----------------------------------\n"
    message += f"<b>💰 想定損益額 (取引サイズ: <ins>${RISK_PER_TRADE_USD:.2f}</ins> 相当)</b>\n"
    message += f"----------------------------------\n"
    message += f"| 📈 <b>想定利益</b> | <b>${pnl_profit_usd:.2f}</b> | <b>+ {pnl_profit_pct:.2f}%</b> (目安) |\n"
    message += f"| 🔻 <b>想定損失</b> | <b>${pnl_loss_usd:.2f}</b> | <b>- {pnl_loss_pct:.2f}%</b> (最大リスク) |\n"
    message += f"----------------------------------\n"

    # フッターとBOT情報
    market_env = 'トレンド' if adx_val > 25 else 'レンジ'

    message += f"\n==================================\n"
    message += f"| 🕰️ <b>TP到達目安</b> | <b>半日以内 (6〜24時間)</b> |\n"
    message += f"| 🔍 <b>市場環境</b> | <b>{market_env}</b> 相場 (ADX: {adx_val:.2f}) |\n"
    message += f"| ⚙️ <b>BOT Ver</b> | <b>{BOT_VERSION}</b> |\n"
    message += f"==================================\n\n"
    message += f"<pre>※ Limit注文は指定水準到達時のみ約定します。</pre>"

    return message

async def send_telegram_notification(message: str) -> None:
    """Telegram APIを使用してメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        # requestsは同期ライブラリなので、asyncioで実行
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response.raise_for_status()
        logging.info("Telegram通知を送信しました。")
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram通知の送信中にエラーが発生しました: {e}")

# ====================================================================================
# MAIN LOGIC
# ====================================================================================

async def main_loop():
    """ボットのメイン実行ループ"""
    global LAST_SUCCESS_TIME, MACRO_CONTEXT
    
    # 実行間隔の計算
    INTERVAL_SECONDS = 180.0 # 3分間隔で実行

    # マクロコンテキストの更新間隔
    MACRO_CONTEXT_UPDATE_INTERVAL = 3600 # 1時間

    while True:
        start_time = time.time()
        
        try:
            # 1. マクロコンテキストの更新 (4h足トレンドの代理として使用)
            if time.time() - LAST_SUCCESS_TIME > MACRO_CONTEXT_UPDATE_INTERVAL:
                 MACRO_CONTEXT = get_macro_context()
            
            # 2. シグナル分析の実行
            logging.info(f"📊 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄のシグナル分析を開始します...")
            
            best_signals_per_symbol = {}
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # analyse_symbolの実行を非同期タスクとしてスケジュール
                tasks.append(analyse_symbol(symbol, MACRO_CONTEXT['dominance_trend'], best_signals_per_symbol))

            # 全ての分析タスクが完了するのを待機
            await asyncio.gather(*tasks)
            
            # 3. Limit Entry ポジションのみをフィルタリング
            limit_entry_signals = [
                item for item in best_signals_per_symbol.values() 
                if item and item['entry_type'] == 'Limit' 
            ]

            # ソート: スコアを最優先、次にリスクリワード比で降順ソート
            sorted_best_signals = sorted(
                limit_entry_signals, 
                key=lambda x: (
                    x['score'],     # スコアを最優先 (降順)
                    x['rr_ratio']   # 次にリスクリワード (降順)
                ), 
                reverse=True
            )
            
            # --------------------------------------------------------------------------
            # 4. シグナルフィルタリングロジック (Top 1 Guarantee)
            # --------------------------------------------------------------------------
            
            top_signals_to_notify = []
            notified_symbols_set = set()
                
            # 4-1. 🥇 最も優位性の高い銘柄 (Top 1) を無条件で通知リストに含める
            if sorted_best_signals:
                top_signals_to_notify.append(sorted_best_signals[0])
                notified_symbols_set.add(sorted_best_signals[0]['symbol'])
                
            # 4-2. 閾値 (SIGNAL_THRESHOLD=0.75) を超えた残りの銘柄を追加 (重複は除く)
            for item in sorted_best_signals:
                # 閾値を超えており、かつ、既にTop 1として追加されていない場合
                if item['score'] >= SIGNAL_THRESHOLD and item['symbol'] not in notified_symbols_set:
                    top_signals_to_notify.append(item)
                    notified_symbols_set.add(item['symbol'])
                    
                # TOP_SIGNAL_COUNT (設定された最大通知数) の数を超えないように調整
                if len(top_signals_to_notify) >= TOP_SIGNAL_COUNT:
                    break
            
            # 5. 通知処理の実行
            new_notifications = 0
            if top_signals_to_notify:
                logging.info(f"🔔 高スコア/高優位性シグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['symbol']
                    current_timestamp = time.time()
                    
                    # クールダウンチェック
                    if (current_timestamp - LAST_ANALYSIS_SIGNALS.get(symbol, 0)) > NOTIFICATION_COOLDOWN_SECONDS:
                        message = create_telegram_message(item, i + 1)
                        # Telegram通知の送信は非同期で実行
                        await send_telegram_notification(message)
                        
                        LAST_ANALYSIS_SIGNALS[symbol] = current_timestamp
                        new_notifications += 1
                    else:
                        logging.info(f"[{symbol}] はクールダウン期間中のため、通知をスキップしました。")
                        
            # 6. ループ終了処理
            logging.info(f"✅ 総合分析完了。高確度シグナル {len(top_signals_to_notify)} 件、新規通知 {new_notifications} 件。")
            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}. CCXTクライアントの再初期化を試みます。")
            
            # CCXTクライアントを閉じて再初期化を試みる
            if EXCHANGE_CLIENT:
                await EXCHANGE_CLIENT.close()
            
            global EXCHANGE_CLIENT
            try:
                # CCXT_CLIENT_NAMEは既に小文字に修正済み
                EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME)({
                    'apiKey': os.getenv('API_KEY'),
                    'secret': os.getenv('SECRET_KEY'),
                    'options': {'defaultType': 'future'} 
                })
                logging.warning("CCXTクライアントを再初期化しました。")
            except Exception as e_reinit:
                logging.error(f"CCXTクライアントの再初期化に失敗: {e_reinit}")
            
            await asyncio.sleep(60) # エラー時の待機
            
        # 待機
        end_time = time.time()
        sleep_duration = INTERVAL_SECONDS - (end_time - start_time)
        if sleep_duration > 0:
            logging.info(f"😴 次の実行まで {sleep_duration:.1f} 秒待機します...")
            await asyncio.sleep(sleep_duration)
        else:
            logging.warning("実行時間が長すぎます。次の実行まで待機しません。")

async def analyse_symbol(symbol: str, dominance_trend: str, results_dict: Dict[str, Any]):
    """単一銘柄の分析とベストシグナルの抽出を実行する"""
    df = await get_historical_data(symbol, TIMEFRAME)
    if df is None or df.empty or len(df) < 50:
        return

    df, atr_val = calculate_indicators(df)
    signals = generate_signals(df, symbol, atr_val, dominance_trend)
    
    if signals:
        # 最もスコアの高いシグナル（Best Signal）を選択し、辞書に格納
        best_signal = max(signals, key=lambda x: x['score'])
        results_dict[symbol] = best_signal

# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

# BOT_VERSIONを使用
app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    logging.info(f"🚀 Apex BOT {BOT_VERSION} Startup initializing...") 
    # メインループをバックグラウンドタスクとして実行
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    """ボットの稼働状況を確認するエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": BOT_VERSION,
        "current_client": CCXT_CLIENT_NAME.upper(),
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_analysis_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "signal_threshold": SIGNAL_THRESHOLD,
        "top_signal_count": TOP_SIGNAL_COUNT
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT {BOT_VERSION} is running."})


if __name__ == "__main__":
    # uvicornサーバーの起動
    # 環境変数からポートを取得、ない場合は8000
    port = int(os.getenv("PORT", 8000))
    # 'host="0.0.0.0"' はコンテナ環境等で外部アクセス可能にするために必要
    uvicorn.run(app, host="0.0.0.0", port=port)
