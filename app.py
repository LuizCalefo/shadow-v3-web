from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
import requests
import pandas as pd
import sqlite3
import os
import threading
import time
from openai import OpenAI
from datetime import datetime
import pytz
import telebot

app = Flask(__name__)
CORS(app)

GROQ_KEY = os.environ.get("GROQ_API_KEY")
client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_KEY)
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FUSO_LISBOA = pytz.timezone('Europe/Lisbon')

DB_FILE = 'trading_shadow.db'
ULTIMA_NOTICIA_PROCESSADA = ""
ULTIMO_RELATORIO_DIARIO = ""
ULTIMO_RELATORIO_SEMANAL = ""
CACHE_MACRO = {}

bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ==========================================
# INICIALIZAÇÃO DA BASE DE DADOS (SQLITE)
# ==========================================
def inicializar_bd():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historico (
                id TEXT PRIMARY KEY,
                ativo TEXT,
                estrategia TEXT,
                entrada REAL,
                tp1 REAL,
                tp2 REAL,
                tp3 REAL,
                sl REAL,
                resultado TEXT,
                estado_fechado INTEGER,
                data_fecho TEXT,
                pontos REAL
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erro ao criar BD SQLite:", e)

inicializar_bd()

def ler_historico():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM historico")
        rows = cursor.fetchall()
        conn.close()
        
        hist = []
        for row in rows:
            hist.append({
                "id": row["id"],
                "ativo": row["ativo"],
                "estrategia": row["estrategia"],
                "entrada": row["entrada"],
                "tp1": row["tp1"],
                "tp2": row["tp2"],
                "tp3": row["tp3"],
                "sl": row["sl"],
                "resultado": row["resultado"],
                "estado_fechado": bool(row["estado_fechado"]),
                "data_fecho": row["data_fecho"],
                "pontos": row["pontos"]
            })
        return hist
    except Exception as e:
        print("Erro ao ler BD:", e)
        return []

def salvar_historico(dados_lista):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM historico")
        for t in dados_lista:
            cursor.execute('''
                INSERT OR REPLACE INTO historico (id, ativo, estrategia, entrada, tp1, tp2, tp3, sl, resultado, estado_fechado, data_fecho, pontos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                t.get("id"),
                t.get("ativo"),
                t.get("estrategia"),
                t.get("entrada"),
                t.get("tp1"),
                t.get("tp2"),
                t.get("tp3"),
                t.get("sl"),
                t.get("resultado", "WAIT"),
                1 if t.get("estado_fechado") else 0,
                t.get("data_fecho"),
                t.get("pontos", 0.0)
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erro ao salvar na BD:", e)

# ==========================================
# SISTEMA DE RETRY AUTOMÁTICO NAS APIS
# ==========================================
def requisicao_com_retry(url, max_tentativas=3, espera=2):
    for tentativa in range(max_tentativas):
        try:
            resposta = requests.get(url, timeout=10)
            if resposta.status_code == 200:
                return resposta.json()
        except Exception as e:
            print(f"Tentativa {tentativa+1} falhou para {url}: {e}")
        time.sleep(espera)
    return None

def chamar_ia_com_retry(prompt, max_tentativas=3):
    for tentativa in range(max_tentativas):
        try:
            res = client.chat.completions.create(
                model="llama-3.1-8b-instant", 
                messages=[{"role": "user", "content": prompt}], 
                temperature=0.3
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            print(f"Tentativa IA {tentativa+1} falhou: {e}")
            time.sleep(2)
    return "⚠️ Serviço de IA temporariamente indisponível."

def enviar_telegram(mensagem):
    if not bot or not TELEGRAM_CHAT_ID: return
    try:
        bot.send_message(TELEGRAM_CHAT_ID, mensagem, parse_mode="Markdown")
    except Exception as e:
        print("Erro Telegram:", e)

# ==========================================
# COMANDOS INTERATIVOS DO TELEGRAM
# ==========================================
if bot:
    @bot.message_handler(commands=['status'])
    def cmd_status(message):
        agora = datetime.now(FUSO_LISBOA)
        is_killzone = 8 <= agora.hour <= 17
        zona = "🟢 Killzone Institucional (Londres/NY)" if is_killzone else "🔴 Baixo Volume / Ásia"
        tendencia_xau = obter_tendencia_macro("XAU/USD")
        
        msg = (f"🤖 *MOTOR QUANTITATIVO ONLINE*\n\n"
               f"🕒 *Hora local:* {agora.strftime('%H:%M')} (Lisboa)\n"
               f"📊 *Sessão Atual:* {zona}\n"
               f"📈 *Macro Tendência Ouro:* {tendencia_xau}\n"
               f"📡 *Varredura:* Ativa (Base SQLite Segura)")
        bot.reply_to(message, msg, parse_mode="Markdown")

    @bot.message_handler(commands=['abertas'])
    def cmd_abertas(message):
        hist = ler_historico()
        abertas = [t for t in hist if not t.get("estado_fechado")]
        
        if not abertas:
            bot.reply_to(message, "💤 *Nenhuma operação aberta neste momento.*", parse_mode="Markdown")
            return
            
        msg = "📂 *OPERAÇÕES EM ANDAMENTO:*\n\n"
        for t in abertas:
            estado = t.get('resultado', 'WAIT')
            icone = "⏳" if estado == "WAIT" else "🛡️" if estado == "BREAKEVEN" else "🔥"
            msg += f"{icone} *{t['ativo']}* ({t['estrategia']})\n  └ Entrada: {t['entrada']} | Fase: {estado}\n\n"
        
        bot.reply_to(message, msg, parse_mode="Markdown")

    @bot.message_handler(commands=['placar'])
    def cmd_placar(message):
        agora = datetime.now(FUSO_LISBOA)
        data_hoje_str = agora.strftime("%Y-%m-%d")
        
        hist = ler_historico()
        # CORRIGIDO AQUI (Removido o colchete a mais)
        hist_hoje = [t for t in hist if t.get("estado_fechado") and t.get("data_fecho") == data_hoje_str]
        
        if not hist_hoje:
            bot.reply_to(message, "📉 *Nenhuma operação fechada hoje.*", parse_mode="Markdown")
            return
            
        w, l, z, pts, alvs, wr = compilar_estatisticas(hist_hoje)
        msg = (f"📊 *PLACAR DE HOJE AO VIVO*\n\n"
               f"⚖️ *Placar:* {w} Wins | {l} Loss | {z} Zero\n"
               f"💰 *Pontos Capturados:* {pts} pts\n"
               f"🎯 *Assertividade:* {wr}%\n")
        bot.reply_to(message, msg, parse_mode="Markdown")

    @bot.message_handler(commands=['varrer'])
    def cmd_varrer(message):
        bot.reply_to(message, "🔍 *Forçando varredura imediata...* aguarde.", parse_mode="Markdown")
        resumos = []
        for ativo in ["XAU", "BTC", "EUR"]:
            dados = analisar_ativo_interno(ativo)
            status = dados.get('status')
            if status == "SETUP_CONFIRMADO":
                resumos.append(f"🟢 *{ativo}:* Oportunidade detetada!")
            elif status == "MERCADO_FECHADO":
                resumos.append(f"💤 *{ativo}:* Mercado Fechado.")
            else:
                resumos.append(f"🔴 *{ativo}:* Sem setup no momento.")
        
        bot.send_message(message.chat.id, "\n".join(resumos), parse_mode="Markdown")

# ==========================================
# MOTOR MACROECONÓMICO COM RETRY NA IA
# ==========================================
def analisar_noticia_ia(titulo_evento, tipo="alerta"):
    if tipo == "alerta":
        prompt = f"Notícia macroeconómica ({titulo_evento}) vai sair agora. Escreva alerta curto (máx 3 linhas) para traders de Ouro e Dólar: Risco de volatilidade e spread. Use emojis."
    else:
        prompt = f"Notícia macroeconómica ({titulo_evento}) publicada nos EUA. Como gestor quantitativo, explique em 5 linhas o impacto no DXY e XAU/USD, cenário forte vs fraco e postura no gráfico. Use emojis."
    
    return chamar_ia_com_retry(prompt)

def verificar_noticias_ao_vivo():
    global ULTIMA_NOTICIA_PROCESSADA
    try:
        agora = datetime.now(FUSO_LISBOA)
        hora_atual = agora.strftime("%H")
        minuto_atual = agora.minute

        if 25 <= minuto_atual <= 28 and hora_atual != ULTIMA_NOTICIA_PROCESSADA:
            analise_live = analisar_noticia_ia(f"Boletim Macro USD - {hora_atual}:30", tipo="alerta")
            enviar_telegram(f"🚨 *ALERTA DE VOLATILIDADE (PRÉ-NOTÍCIA)* 🚨\n\n{analise_live}")
            
        elif 35 <= minuto_atual <= 38 and hora_atual != ULTIMA_NOTICIA_PROCESSADA:
            analise_pos = analisar_noticia_ia(f"Rescaldo Macro USD - {hora_atual}:30", tipo="pos")
            enviar_telegram(f"📊 *ANÁLISE PÓS-NOTÍCIA (IMPACTO NO GRÁFICO)* 📊\n\n{analise_pos}")
            ULTIMA_NOTICIA_PROCESSADA = hora_atual
    except Exception as e:
        print("Erro nas notícias:", e)

def compilar_estatisticas(hist_filtrado):
    wins = losses = zeros = pontos_totais = 0
    alvos = {"TP1": 0, "TP3": 0} 
    for t in hist_filtrado:
        res = t.get("resultado")
        if res == "WIN":
            wins += 1
            alvo_atingido = t.get("alvo", "TP1")
            if alvo_atingido in alvos: alvos[alvo_atingido] += 1
        elif res == "LOSS": losses += 1
        elif res == "ZERO": zeros += 1
        pontos_totais += t.get("pontos", 0.0)

    total = wins + losses + zeros
    win_rate = round((wins / total * 100), 1) if total > 0 else 0
    return wins, losses, zeros, round(pontos_totais, 1), alvos, win_rate

def verificar_relatorios():
    global ULTIMO_RELATORIO_DIARIO, ULTIMO_RELATORIO_SEMANAL
    try:
        agora = datetime.now(FUSO_LISBOA)
        data_hoje_str = agora.strftime("%Y-%m-%d")
        hist = ler_historico()
        hist_fechado = [t for t in hist if t.get("estado_fechado") == True]

        if agora.hour == 21 and 50 <= agora.minute <= 59 and data_hoje_str != ULTIMO_RELATORIO_DIARIO:
            hist_hoje = [t for t in hist_fechado if t.get("data_fecho") == data_hoje_str]
            if hist_hoje:
                w, l, z, pts, alvs, wr = compilar_estatisticas(hist_hoje)
                enviar_telegram(f"📅 *FECHO DO DIÁRIO*\n⚖️ Wins: {w} | Loss: {l}\n💰 Pontos: {pts} pts")
            ULTIMO_RELATORIO_DIARIO = data_hoje_str

        if agora.weekday() == 4 and agora.hour == 22 and 0 <= agora.minute <= 10 and data_hoje_str != ULTIMO_RELATORIO_SEMANAL:
            semana_atual = agora.isocalendar()[1]
            hist_semana = [t for t in hist_fechado if t.get("data_fecho") and datetime.strptime(t.get("data_fecho"), "%Y-%m-%d").isocalendar()[1] == semana_atual]
            if hist_semana:
                w, l, z, pts, alvs, wr = compilar_estatisticas(hist_semana)
                comentario_ia = chamar_ia_com_retry(f"Resumo semanal: {w} Wins, {l} Losses, {pts} pts, {wr}% assertividade. Análise profissional de 3 linhas.")
                enviar_telegram(f"📊 *BALANÇO SEMANAL*\nWins: {w} | Losses: {l} | Total: {pts} pts\n\n\"{comentario_ia}\"")
            ULTIMO_RELATORIO_SEMANAL = data_hoje_str
    except: pass

def obter_tendencia_macro(simbolo):
    agora_timestamp = time.time()
    if simbolo in CACHE_MACRO and (agora_timestamp - CACHE_MACRO[simbolo]['timestamp']) < 3600:
        return CACHE_MACRO[simbolo]['tendencia']
    try:
        req_1h = requisicao_com_retry(f"https://api.twelvedata.com/time_series?symbol={simbolo}&interval=1h&outputsize=25&apikey={TWELVEDATA_KEY}")
        req_4h = requisicao_com_retry(f"https://api.twelvedata.com/time_series?symbol={simbolo}&interval=4h&outputsize=25&apikey={TWELVEDATA_KEY}")
        if not req_1h or not req_4h or "values" not in req_1h or "values" not in req_4h: 
            return CACHE_MACRO.get(simbolo, {}).get('tendencia', 'LATERAL')
            
        df1 = pd.DataFrame(req_1h['values'])[::-1].reset_index(drop=True)
        df1['close'] = df1['close'].astype(float)
        ema9_1h, ema21_1h = df1['close'].ewm(span=9, adjust=False).mean().iloc[-1], df1['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        
        df4 = pd.DataFrame(req_4h['values'])[::-1].reset_index(drop=True)
        df4['close'] = df4['close'].astype(float)
        ema9_4h, ema21_4h = df4['close'].ewm(span=9, adjust=False).mean().iloc[-1], df4['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        
        tendencia = "ALTA" if ema9_1h > ema21_1h and ema9_4h > ema21_4h else "BAIXA" if ema9_1h < ema21_1h and ema9_4h < ema21_4h else "LATERAL"
        CACHE_MACRO[simbolo] = {'tendencia': tendencia, 'timestamp': agora_timestamp}
        return tendencia
    except: 
        return CACHE_MACRO.get(simbolo, {}).get('tendencia', 'LATERAL')

def calcular_lote(ativo, entrada, stop_loss):
    distancia = abs(entrada - stop_loss)
    try:
        if ativo == "XAU/USD": return round(10.0 / (distancia * 100), 2)
        elif ativo == "EUR/USD": return round(10.0 / (distancia * 100000), 2)
        elif ativo == "BTC/USD": return round(10.0 / (distancia * 1), 3)
    except: return 0.01
    return 0.01

def analisar_ativo_interno(ativo):
    simbolo = "XAU/USD"
    if ativo == "BTC": simbolo = "BTC/USD"
    elif ativo == "EUR": simbolo = "EUR/USD"

    agora = datetime.now(FUSO_LISBOA)
    if ativo in ["XAU", "EUR"]:
        if (agora.weekday() == 4 and agora.hour >= 21) or agora.weekday() == 5 or (agora.weekday() == 6 and agora.hour < 22):
            return {"status": "MERCADO_FECHADO"}

    is_killzone = 8 <= agora.hour <= 17
    zona_operacional = "🟢 Killzone Institucional (Londres/NY)" if is_killzone else "🔴 Baixo Volume / Ásia"

    resp_dados = requisicao_com_retry(f"https://api.twelvedata.com/time_series?symbol={simbolo}&interval=15min&outputsize=50&apikey={TWELVEDATA_KEY}")
    if not resp_dados or "values" not in resp_dados: return {"status": "ERRO_API"}

    df = pd.DataFrame(resp_dados["values"])
    df = df.iloc[::-1].reset_index(drop=True) 
    for col in ['close', 'high', 'low', 'open']: df[col] = df[col].astype(float)
    
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df.apply(lambda x: max(x['high'] - x['low'], abs(x['high'] - x['prev_close']), abs(x['low'] - x['prev_close'])), axis=1)
    df['atr'] = df['tr'].rolling(window=14).mean()

    candle_atual, candle_anterior = df.iloc[-1], df.iloc[-2]
    casas_dec = 5 if ativo == "EUR" else 2
    preco_atual, ema_9_atual, ema_21_atual, atr_atual = round(candle_atual['close'], casas_dec), candle_atual['ema_9'], candle_atual['ema_21'], candle_atual['atr']

    status_setup, tp1, tp2, tp3, sl, estrategia, prob = False, 0.0, 0.0, 0.0, 0.0, "", 0
    tendencia = obter_tendencia_macro(simbolo)
    
    if ema_9_atual > ema_21_atual and tendencia == "ALTA":
        if -atr_atual <= (candle_atual['low'] - ema_21_atual) <= atr_atual:
            status_setup, estrategia, prob = True, "PULLBACK FLEXÍVEL (COMPRA)", 82 if is_killzone else 70
        elif candle_anterior['close'] < candle_anterior['ema_9'] and preco_atual > ema_9_atual:
            status_setup, estrategia, prob = True, "MOMENTUM SCALPER (COMPRA)", 75 if is_killzone else 65
        if status_setup:
            sl = round(preco_atual - (1.5 * atr_atual), casas_dec)
            tp1, tp2, tp3 = [round(preco_atual + (m * atr_atual), casas_dec) for m in [1.0, 2.0, 3.0]]

    elif ema_9_atual <= ema_21_atual and tendencia == "BAIXA":
        if candle_atual['high'] >= ema_9_atual and candle_atual['close'] < ema_9_atual:
            status_setup, estrategia, prob = True, "REJEIÇÃO DE TOPO (VENDA)", 78 if is_killzone else 68
            sl = round(preco_atual + (1.5 * atr_atual), casas_dec)
            tp1, tp2, tp3 = [round(preco_atual - (m * atr_atual), casas_dec) for m in [1.0, 2.0, 3.0]]

    resultado = {"ativo": simbolo.replace("/", ""), "preco_atual": preco_atual, "atr_atual": round(atr_atual, casas_dec), "status": "SEM_SETUP"}
    if status_setup:
        resultado.update({"status": "SETUP_CONFIRMADO", "estrategia_ativa": estrategia, "entrada": preco_atual, "stop_loss": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "probabilidade": f"{prob}%", "tendencia_macro": tendencia, "zona_operacional": zona_operacional, "lote_sugerido": calcular_lote(simbolo, preco_atual, sl), "data_hora": resp_dados["values"][0]["datetime"]})
    return resultado

def fechar_trade_contabilidade(trade, resultado, alvo, preco_saida):
    trade["resultado"], trade["estado_fechado"], trade["data_fecho"], trade["alvo"] = resultado, True, datetime.now(FUSO_LISBOA).strftime("%Y-%m-%d"), alvo
    is_compra = trade["tp1"] > trade["entrada"]
    diff = preco_saida - trade["entrada"] if is_compra else trade["entrada"] - preco_saida
    pts = diff * 100000 if "EUR" in trade["ativo"] else diff * 100 if "XAU" in trade["ativo"] else diff
    trade["pontos"] = round(pts, 1)

def avaliar_operacoes_abertas_autonomo(preco_atual, ativo_formatado):
    hist = ler_historico()
    mudou = False
    for trade in hist:
        if trade.get("estado_fechado"): continue
        is_compra = trade["tp1"] > trade["entrada"]

        if trade["resultado"] == "WAIT" and trade["ativo"] == ativo_formatado:
            if is_compra:
                if preco_atual >= trade["tp3"]: fechar_trade_contabilidade(trade, "WIN", "TP3", trade["tp3"]); mudou = True; enviar_telegram(f"🎯 *TP3 ALCANÇADO (+{trade['pontos']} pts)*\n*{ativo_formatado}*")
                elif preco_atual >= trade["tp1"]: trade["resultado"] = "BREAKEVEN"; mudou = True; enviar_telegram(f"🏆 *BREAKEVEN (TP1)*\n*{ativo_formatado}* Risco anulado.")
                elif preco_atual <= trade["sl"]: fechar_trade_contabilidade(trade, "LOSS", "SL", trade["sl"]); mudou = True; enviar_telegram(f"❌ *LOSS* em {ativo_formatado}")
            else: 
                if preco_atual <= trade["tp3"]: fechar_trade_contabilidade(trade, "WIN", "TP3", trade["tp3"]); mudou = True; enviar_telegram(f"🎯 *TP3 ALCANÇADO (+{trade['pontos']} pts)*\n*{ativo_formatado}*")
                elif preco_atual <= trade["tp1"]: trade["resultado"] = "BREAKEVEN"; mudou = True; enviar_telegram(f"🏆 *BREAKEVEN (TP1)*\n*{ativo_formatado}* Risco anulado.")
                elif preco_atual >= trade["sl"]: fechar_trade_contabilidade(trade, "LOSS", "SL", trade["sl"]); mudou = True; enviar_telegram(f"❌ *LOSS* em {ativo_formatado}")
        
        elif trade["resultado"] == "BREAKEVEN" and trade["ativo"] == ativo_formatado:
            if is_compra:
                if preco_atual >= trade["tp3"]: fechar_trade_contabilidade(trade, "WIN", "TP3", trade["tp3"]); mudou = True
                elif preco_atual >= trade["tp2"]: trade["resultado"] = "TRAILING_TP1"; mudou = True
                elif preco_atual <= trade["entrada"]: fechar_trade_contabilidade(trade, "ZERO", "ZERO", trade["entrada"]); mudou = True
            else:
                if preco_atual <= trade["tp3"]: fechar_trade_contabilidade(trade, "WIN", "TP3", trade["tp3"]); mudou = True
                elif preco_atual <= trade["tp2"]: trade["resultado"] = "TRAILING_TP1"; mudou = True
                elif preco_atual >= trade["entrada"]: fechar_trade_contabilidade(trade, "ZERO", "ZERO", trade["entrada"]); mudou = True

    if mudou: salvar_historico(hist)

def motor_quantitativo_loop():
    while True:
        try:
            verificar_noticias_ao_vivo()
            verificar_relatorios()
            for ativo in ["XAU", "BTC", "EUR"]:
                dados = analisar_ativo_interno(ativo)
                if dados.get("status") in ["MERCADO_FECHADO", "ERRO_API"]: continue
                if dados.get("preco_atual"): avaliar_operacoes_abertas_autonomo(dados["preco_atual"], dados["ativo"])
                if dados.get("status") == "SETUP_CONFIRMADO":
                    id_atual = dados["ativo"] + dados["estrategia_ativa"] + dados["data_hora"]
                    hist = ler_historico()
                    if not any(t["id"] == id_atual for t in hist):
                        msg = (f"⚡ *SINAL INSTITUCIONAL*\n🪙 Ativo: {dados['ativo']}\n🎯 Estratégia: {dados['estrategia_ativa']}\n\n🟢 Entrada: {dados['entrada']}\n🔴 SL: {dados['stop_loss']}\n✅ TP1: {dados['tp1']}")
                        enviar_telegram(msg)
                        hist.append({"ativo": dados["ativo"], "estrategia": dados["estrategia_ativa"], "entrada": dados["entrada"], "tp1": dados["tp1"], "tp2": dados["tp2"], "tp3": dados["tp3"], "sl": dados["stop_loss"], "resultado": "WAIT", "estado_fechado": False, "id": id_atual})
                        salvar_historico(hist)
        except Exception as e: print("Erro no loop:", e)
        time.sleep(300)

# ==========================================
# THREADS
# ==========================================
threading.Thread(target=motor_quantitativo_loop, daemon=True).start()
if bot:
    threading.Thread(target=bot.infinity_polling, daemon=True).start()

# ==========================================
# ROTAS DO FLASK / API DO DASHBOARD
# ==========================================
@app.route('/analisar', methods=['GET'])
def analisar_api():
    ativo = request.args.get('ativo', 'XAU')
    teste = request.args.get('teste', 'false').lower() == 'true'
    
    if teste:
        return jsonify({
            "status": "SETUP_CONFIRMADO",
            "ativo": f"{ativo}USD",
            "estrategia_ativa": "TESTE DE SISTEMA INTEGRADO",
            "entrada": 2350.50 if ativo == "XAU" else 65000.00,
            "stop_loss": 2345.00 if ativo == "XAU" else 64500.00,
            "tp1": 2356.00 if ativo == "XAU" else 65500.00,
            "tp2": 2362.00 if ativo == "XAU" else 66000.00,
            "tp3": 2370.00 if ativo == "XAU" else 67000.00,
            "atr_atual": 12.5,
            "probabilidade": "90%",
            "explicacao_estrategia": "Teste operacional da base SQLite e rotas seguras.",
            "data_hora": datetime.now(FUSO_LISBOA).strftime("%Y-%m-%d %H:%M:%S")
        })

    resultado = analisar_ativo_interno(ativo)
    return jsonify(resultado)

@app.route('/radar-mensal', methods=['GET'])
def radar_mensal():
    prompt = "Resumo macroeconómico (máx 3 frases) sobre expectativas FED e Payroll para Ouro e Dólar."
    res = chamar_ia_com_retry(prompt)
    return jsonify({"radar": res})

@app.route('/ping', methods=['GET'])
def ping(): 
    return jsonify({"status": "motor_sqlite_online"})

@app.route('/get-historico', methods=['GET'])
def get_historico(): 
    return jsonify(ler_historico())

@app.route('/sync-historico', methods=['POST'])
def sync_historico():
    salvar_historico(request.json)
    return jsonify({"status": "sucesso"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)