from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from fastapi.responses import RedirectResponse
from typing import Optional, Dict, List
import requests
import json
import asyncio
from datetime import datetime, timedelta
import sqlite3
import pandas as pd
from enum import Enum
import re  # ADICIONADO para normalização de telefones

# IMPORTS PARA .ENV 
import os
from dotenv import load_dotenv

app = FastAPI(title="Previdas Automation Engine", version="1.0.0")

# CORS para permitir requisições do frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurar arquivos estáticos e templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ==================== CONFIGURAÇÕES ====================
# Carregar variáveis do arquivo .env
load_dotenv()

# Buscar chave do ambiente (nunca hardcoded)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Configurar cliente OpenAI de forma segura
if OPENAI_API_KEY and OPENAI_API_KEY.startswith("sk-"):
    try:
        from openai import AsyncOpenAI
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        print(f"✅ OpenAI configurada com chave: {OPENAI_API_KEY[:15]}...")
    except ImportError:
        openai_client = None
        print("⚠️ OpenAI não instalada - usando fallback")
else:
    openai_client = None
    print("⚠️ OpenAI não configurada - usando fallback")

# URLs dos sistemas exemplos
CRM_API_URL = "https://api.seu-crm.com"
WHATSAPP_API_URL = "https://api.whatsapp.business"
EMAIL_API_URL = "https://api.activecampaign.com"

# ==================== FUNÇÃO CRÍTICA: NORMALIZAÇÃO DE TELEFONES ====================
def normalize_phone(phone: str) -> str:
    """
    Normaliza telefones para formato único - SOLUÇÃO PARA DUPLICAÇÃO
    
    Exemplos:
    - "+31 619 255 082" → "619255082"
    - "(31) 61925-5082" → "619255082"  
    - "31619255082" → "619255082"
    """
    if not phone:
        return ""
    
    # Remove TODOS os caracteres não numéricos
    clean = re.sub(r'[^\d]', '', str(phone))
    
    # Remove código do país Holanda (31) se presente
    if clean.startswith('31') and len(clean) > 10:
        clean = clean[2:]
    
    # Remove zeros à esquerda se existirem
    clean = clean.lstrip('0')
    
    print(f"📞 Telefone normalizado: '{phone}' → '{clean}'")
    return clean

# ==================== MODELOS PYDANTIC ====================
class LeadStatus(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    QUALIFIED = "qualified"
    CUSTOMER = "customer"

class Lead(BaseModel):
    phone: str
    name: Optional[str] = None
    message: str
    source: str
    status: LeadStatus = LeadStatus.COLD
    score: int = 0
    created_at: Optional[datetime] = None

class ChatMessage(BaseModel):
    phone: str
    message: str
    is_bot: bool = False
    timestamp: Optional[datetime] = None

class AutomationTrigger(BaseModel):
    trigger_type: str  # "new_lead", "message_received", "status_changed"
    data: Dict
    conditions: Optional[Dict] = None

# ==================== BANCO DE DADOS CORRIGIDO ====================
def init_db():
    conn = sqlite3.connect('previdas.db')
    cursor = conn.cursor()
    
    # Tabela de leads
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            name TEXT,
            status TEXT,
            score INTEGER,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabela de conversas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            message TEXT,
            is_bot BOOLEAN,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (phone) REFERENCES leads (phone)
        )
    ''')
    
    # Tabela de automações
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS automation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_type TEXT,
            phone TEXT,
            action_taken TEXT,
            result TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

# ==================== IA CORRIGIDA COM PROMPTS MELHORES ====================
class AIService:
    @staticmethod
    async def analyze_message(message: str, context: Dict = None) -> Dict:
        """Análise CORRIGIDA com prompts específicos para Previdas"""
        
        # PROMPT COMPLETAMENTE REFORMULADO
        prompt = f"""Você é um especialista em qualificação de leads para PREVIDAS (laudos médicos para advogados).

REGRAS ESPECÍFICAS PARA SCORING:

🏥 PRODUTOS ESPECÍFICOS (+30 pontos cada):
- "BPC" = Benefício de Prestação Continuada
- "laudo" ou "perícia" = produto direto
- "previdenciário" / "trabalhista" = especialidades

👨‍⚖️ IDENTIFICAÇÃO PROFISSIONAL:
- "advogado" = +40 pontos
- "escritório" / "casos" / "clientes" = +30 pontos
- "doutor" / "especialista" = +25 pontos

⚡ URGÊNCIA:
- "urgente" + contexto (audiência/prazo) = +25 pontos
- "preciso" / "necessito" = +15 pontos
- "hoje" / "amanhã" = +20 pontos

EXEMPLOS CORRETOS DE SCORING:
- "preciso do laudo BPC" = 75 pontos (produto específico + urgência)
- "sou advogado previdenciário" = 85 pontos (profissão + especialidade)
- "trabalham com que?" = 25 pontos (pergunta vaga)
- "oi" = 10 pontos (irrelevante)

RESPONDA APENAS JSON:
{{"intent": "valor", "urgency": "valor", "score": número, "next_action": "valor", "sentiment": "valor"}}

VALORES PERMITIDOS:
- intent: "lawyer", "urgent_case", "product_inquiry", "price_inquiry", "casual", "unclear"
- urgency: "high", "medium", "low"
- score: 0-100
- next_action: "transfer_sales", "nurture", "collect_info", "qualify_more"
- sentiment: "positive", "neutral", "negative"

Mensagem: "{message}"

JSON:"""
        
        try:
            if openai_client:
                print(f"🤖 Analisando: {message[:50]}...")
                
                response = await openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=150,
                    response_format={"type": "json_object"}
                )
                
                result = json.loads(response.choices[0].message.content)
                print(f"✅ OpenAI CORRIGIDA: {result}")
                return result
            else:
                raise Exception("OpenAI não configurada")
                
        except Exception as e:
            print(f"❌ Erro IA: {e}")
            
            # FALLBACK CORRIGIDO COM LÓGICA MELHORADA
            message_lower = message.lower()
            score = 20  # Score base mais alto
            
            # PRODUTOS ESPECÍFICOS (prioridade máxima)
            if "bpc" in message_lower:
                score += 30  # BPC é produto específico
            if "laudo" in message_lower or "perícia" in message_lower:
                score += 30  # Produto direto
            if "previdenciário" in message_lower or "trabalhista" in message_lower:
                score += 25  # Especialidade específica
            
            # IDENTIFICAÇÃO PROFISSIONAL
            if "advogado" in message_lower:
                score += 40  # Profissão target
                if any(x in message_lower for x in ["especialista", "especializado"]):
                    score += 20  # Advogado especialista
            if any(x in message_lower for x in ["escritório", "casos", "clientes"]):
                score += 25  # Contexto profissional
            
            # URGÊNCIA E NECESSIDADE
            if "preciso" in message_lower or "necessito" in message_lower:
                score += 15  # Demonstra necessidade
            if "urgente" in message_lower:
                score += 20  # Urgência
            if any(x in message_lower for x in ["hoje", "amanhã", "audiência"]):
                score += 15  # Urgência contextual
            
            # PENALIZAÇÕES REDUZIDAS
            if len(message_lower) < 8:
                score -= 5  # Penalização menor para mensagens curtas
            
            if message_lower in ["oi", "olá", "hello", "hey", "e ai"]:
                score = 15  # Cumprimento básico
            
            # Determinar intenção baseada no score E conteúdo
            if "advogado" in message_lower or score >= 70:
                intent = "lawyer"
            elif any(x in message_lower for x in ["laudo", "bpc", "perícia"]):
                intent = "product_inquiry"
            elif any(x in message_lower for x in ["preço", "valor", "custo"]):
                intent = "price_inquiry"
            elif score >= 40:
                intent = "unclear"
            else:
                intent = "casual"
            
            result = {
                "intent": intent,
                "urgency": "high" if any(x in message_lower for x in ["urgente", "hoje", "amanhã"]) else "medium" if score >= 50 else "low",
                "score": max(10, min(100, score)),  # Mínimo de 10 pontos
                "next_action": "transfer_sales" if score >= 75 else "nurture" if score >= 50 else "qualify_more",
                "sentiment": "positive" if score >= 60 else "neutral"
            }
            
            print(f"🔄 Fallback CORRIGIDO: {result}")
            return result

    @staticmethod
    async def generate_response(message: str, lead_data: Dict, conversation_history: List) -> str:
        """Gera resposta ESPECÍFICA para leads qualificados COM CONTEXTO"""
        
        message_lower = message.lower()
        score = lead_data.get('score', 0)
        status = lead_data.get('status', 'new')
        
        # VERIFICAR SE É LEAD JÁ CONHECIDO COM SCORE ALTO
        is_known_lead = score >= 75 or status == "qualified"
        
        if is_known_lead:
            # RESPOSTAS CONTEXTUAIS PARA LEADS CONHECIDOS
            if "seguro" in message_lower:
                return "Olá! Somos especializados em laudos médicos, não seguros. Mas posso ajudar com laudos para seus processos previdenciários. Precisa de algum laudo médico?"
            elif any(x in message_lower for x in ["banco", "empréstimo", "financiamento"]):
                return "Olá! Nossa especialidade são laudos médicos para processos jurídicos. Como posso ajudar com laudos para seus casos?"
            elif any(x in message_lower for x in ["curso", "treinamento", "capacitação"]):
                return "Olá! Somos especialistas em laudos médicos, não cursos. Mas posso ajudar com laudos para seus processos. Tem algum caso pendente?"
        
        # Respostas específicas para produtos mencionados
        if "bpc" in message_lower:
            if "urgente" in message_lower:
                return "Especialistas em BPC urgente! Emitimos laudos em 6h. Qual o prazo da audiência?"
            else:
                return "Perfeito! Somos especialistas em laudos BPC. Qual o CID do seu cliente?"
        
        elif "laudo" in message_lower:
            if "previdenciário" in message_lower or "trabalhista" in message_lower:
                return "Especialistas nessa área! Quantos laudos você precisa por mês?"
            else:
                return "Fazemos laudos médicos especializados. Qual área: previdenciário, trabalhista ou civil?"
        
        elif "advogado" in message_lower:
            return "Perfeito! Ajudamos advogados com laudos médicos há 10 anos. Qual sua especialidade?"
        
        else:
            # Resposta padrão para leads qualificados
            return "Vou conectar você com nosso especialista imediatamente. Qual o melhor horário para contato?"

    @staticmethod
    async def generate_nurture_response(message: str, lead_data: Dict, conversation_history: List) -> str:
        """Gera resposta de nutrição MELHORADA COM CONTEXTO"""
        
        message_lower = message.lower()
        score = lead_data.get('score', 0)
        
        # Se lead tem score alto mas não foi qualificado, ser mais direto
        if score >= 70:
            if "seguro" in message_lower:
                return "Entendi! Não trabalhamos com seguros, mas somos especialistas em laudos médicos para advogados. Você atua na área jurídica?"
            elif "bpc" in message_lower or "previdenciário" in message_lower:
                return "Somos especialistas em BPC! Nossos laudos têm 95% de aprovação. Conectando com nosso especialista..."
            else:
                return "Entendo! Somos a Previdas, especialistas em laudos médicos para advogados. Vou conectar você com nossa equipe especializada."
        
        # Respostas normais de nutrição
        if "bpc" in message_lower or "previdenciário" in message_lower:
            return "Somos especialistas em BPC! Nossos laudos têm 95% de aprovação. Você é advogado?"
        elif "laudo" in message_lower:
            return "Fazemos laudos médicos para processos jurídicos. Qual sua área de atuação?"
        elif "trabalham" in message_lower and "que" in message_lower:
            return "Laudos médicos especializados para advogados. Você atua com previdenciário ou trabalhista?"
        elif "preço" in message_lower or "valor" in message_lower:
            return "Nossos valores são competitivos. Você trabalha com quantos casos por mês?"
        else:
            return "Entendi. Somos especialistas em laudos médicos para advogados. Qual sua área?"
    
    @staticmethod
    async def generate_qualification_response(message: str, lead_data: Dict, conversation_history: List) -> str:
        """Gera resposta de qualificação APRIMORADA COM CONTEXTO"""
        
        message_lower = message.lower()
        score = lead_data.get('score', 0)
        
        # Se é lead com algum score mas mensagem fora do contexto
        if score >= 50:
            if "seguro" in message_lower:
                return "Olá! Nossa especialidade são laudos médicos para advogados, não seguros. Você trabalha com direito?"
            elif any(x in message_lower for x in ["banco", "empréstimo", "investimento"]):
                return "Olá! Somos especializados em laudos médicos para processos jurídicos. Você é advogado?"
        
        # Respostas normais de qualificação
        if "trabalham" in message_lower and "que" in message_lower:
            return "Fazemos laudos médicos para processos jurídicos. Você é advogado?"
        elif len(message_lower) < 10:
            return "Olá! Somos especialistas em laudos médicos para advogados. Qual sua profissão?"
        else:
            return "Entendido. Somos a Previdas, laudos médicos para advogados. Você atua na área jurídica?"

# ==================== INTEGRAÇÕES CORRIGIDAS ====================
class IntegrationService:
    @staticmethod
    async def send_to_crm(lead_data: Dict) -> bool:
        """Envia/atualiza lead no CRM com telefone normalizado"""
        try:
            # CORREÇÃO: Sempre normalizar telefone
            normalized_phone = normalize_phone(lead_data["phone"])
            
            conn = sqlite3.connect('previdas.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO leads (phone, name, status, score, source, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (normalized_phone, lead_data.get("name"), lead_data["status"], 
                  lead_data["score"], lead_data.get("source", "whatsapp")))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            print(f"❌ Erro ao enviar para CRM: {e}")
            return False

    @staticmethod
    async def send_whatsapp(phone: str, message: str) -> bool:
        """Envia mensagem via WhatsApp Business API"""
        try:
            print(f"📱 WhatsApp para {phone}: {message}")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao enviar WhatsApp: {e}")
            return False

    @staticmethod
    async def notify_sales_team(lead_data: Dict) -> bool:
        """Notifica equipe de vendas sobre lead quente"""
        try:
            message = f"""
🔥 LEAD QUENTE PREVIDAS!
Nome: {lead_data.get('name', 'N/A')}
Phone: {lead_data['phone']}
Score: {lead_data['score']}/100
Status: Lead qualificado para laudos médicos
Ação: Contatar IMEDIATAMENTE!
"""
            
            print(f"🚨 Notificação vendas: {message}")
            return True
            
        except Exception as e:
            print(f"❌ Erro ao notificar vendas: {e}")
            return False

# ==================== ENGINE DE AUTOMAÇÃO CORRIGIDA ====================
class AutomationEngine:
    @staticmethod
    async def process_automation(trigger: AutomationTrigger):
        """Processa automação baseada no trigger"""
        
        if trigger.trigger_type == "new_lead":
            await AutomationEngine._handle_new_lead(trigger.data)
        
        elif trigger.trigger_type == "message_received":
            await AutomationEngine._handle_message(trigger.data)
        
        elif trigger.trigger_type == "status_changed":
            await AutomationEngine._handle_status_change(trigger.data)

    @staticmethod
    async def _handle_new_lead(data: Dict):
        """Automação para novo lead"""
        
        # 1. Normalizar telefone
        normalized_phone = normalize_phone(data["phone"])
        data["phone"] = normalized_phone
        
        # 2. Salva no CRM
        await IntegrationService.send_to_crm(data)
        
        # 3. Envia mensagem de boas-vindas
        welcome_msg = "Olá! Sou da Previdas, especialistas em laudos médicos para advogados. Como posso ajudar?"
        await IntegrationService.send_whatsapp(data["phone"], welcome_msg)
        
        # 4. Log da automação
        AutomationEngine._log_automation("new_lead", normalized_phone, "welcome_sent", "success")

    @staticmethod
    async def _handle_message(data: Dict):
        """AUTOMAÇÃO CORRIGIDA - Lógica de scoring otimizada COM CONTEXTO HISTÓRICO CORRIGIDO"""
        
        # 0. NORMALIZAR TELEFONE (CRÍTICO)
        normalized_phone = normalize_phone(data["phone"])
        data["phone"] = normalized_phone
        
        # 1. Busca dados do lead
        lead_data = AutomationEngine._get_lead_data(normalized_phone)
        
        # 2. Analisa mensagem com IA CORRIGIDA
        analysis = await AIService.analyze_message(data["message"], lead_data)
        
        # 3. LÓGICA DE SCORING COMPLETAMENTE CORRIGIDA
        current_score = lead_data.get("score", 0)
        current_status = lead_data.get("status", "new")
        ai_score = analysis["score"]
        message_lower = data["message"].lower()
        
        print(f"🔍 DEBUG SCORING COM CONTEXTO:")
        print(f"  📊 Current Score: {current_score}")
        print(f"  📋 Current Status: {current_status}")
        print(f"  🤖 AI Score: {ai_score}")
        print(f"  💬 Message: '{data['message']}'")
        
        # PALAVRAS-CHAVE QUE INDICAM QUALIDADE
        product_keywords = ["bpc", "laudo", "perícia", "previdenciário", "trabalhista"]
        professional_keywords = ["advogado", "escritório", "casos", "clientes"]
        urgency_keywords = ["urgente", "preciso", "necessito", "hoje", "amanhã"]
        
        has_product_keywords = any(kw in message_lower for kw in product_keywords)
        has_professional_keywords = any(kw in message_lower for kw in professional_keywords)
        has_urgency_keywords = any(kw in message_lower for kw in urgency_keywords)
        
        # NOVA LÓGICA DE SCORING (SEM DECAY DESNECESSÁRIO)
        if has_product_keywords or has_professional_keywords:
            # Mensagem sobre produtos ou identificação profissional = SEMPRE melhora score
            new_score = max(current_score, ai_score, 70)  # Mínimo 70 para produtos específicos
            print(f"  ✅ PRODUTO/PROFISSIONAL mencionado - Score garantido: {new_score}")
            
        elif ai_score >= 60:
            # Mensagem boa - mantém o melhor score
            new_score = max(current_score, ai_score)
            print(f"  ✅ Mensagem BOA - Score: {new_score}")
            
        elif ai_score >= 40:
            # Mensagem neutra - score ponderado suave
            new_score = int((current_score * 0.85) + (ai_score * 0.15))
            print(f"  🟡 Mensagem NEUTRA - Score ponderado: {new_score}")
            
        else:
            # Mensagem ruim - decay muito limitado
            if len(data["message"]) < 6 and not any(kw in message_lower for kw in ["oi", "olá", "hey"]):
                # Apenas mensagens muito ruins e curtas recebem decay
                new_score = max(current_score - 10, current_score * 0.9, 20)  # Redução máxima de 10 pontos
                print(f"  ❌ Mensagem RUIM - Decay limitado: {new_score}")
            else:
                # Cumprimentos normais não recebem penalização
                new_score = current_score
                print(f"  😐 Cumprimento/Mensagem normal - Score mantido: {new_score}")
        
        # Garantir limites
        new_score = max(10, min(100, int(new_score)))
        lead_data["score"] = new_score
        
        print(f"📊 RESULTADO FINAL: {current_score} → {new_score} (IA: {ai_score})")
        
        # 4. LÓGICA DE QUALIFICAÇÃO COM CONTEXTO HISTÓRICO (CORREÇÃO FINAL)
        has_quality_keywords = has_product_keywords or has_professional_keywords or has_urgency_keywords
        
        print(f"🔍 Keywords: Produto={has_product_keywords}, Profissional={has_professional_keywords}, Urgência={has_urgency_keywords}")
        
        # VERIFICAR CONTEXTO HISTÓRICO PRIMEIRO (PRIORIDADE MÁXIMA)
        already_qualified = current_status == "qualified"
        has_high_historical_score = new_score >= 80
        
        # LÓGICA CORRIGIDA: CONTEXTO HISTÓRICO TEM PRIORIDADE ABSOLUTA
        if already_qualified and ai_score >= 20:
            # Lead já qualificado + mensagem não muito negativa = MANTER QUALIFICAÇÃO
            lead_data["status"] = "qualified"
            final_status = "qualified"
            print(f"🔄 Lead qualificado MANTIDO (contexto histórico: {current_status})")
            
        elif has_high_historical_score and ai_score >= 30:
            # Lead com score alto histórico + mensagem não muito negativa = RE-QUALIFICAR
            lead_data["status"] = "qualified"
            final_status = "qualified"
            print(f"🔄 Lead RE-QUALIFICADO por score histórico alto ({new_score})")
            
        else:
            # APENAS AQUI aplicar lógica normal para leads novos ou com score baixo
            is_hot_lead = (
                new_score >= 75 and
                (has_quality_keywords or analysis["intent"] in ["lawyer", "product_inquiry"]) and
                len(data["message"]) > 5
            )
            
            if is_hot_lead:
                lead_data["status"] = "qualified"
                final_status = "qualified"
                print(f"🔥 Lead NOVA qualificação! Score: {new_score}")
            elif new_score >= 50 and has_quality_keywords:
                lead_data["status"] = "warm"
                final_status = "warm"
                print(f"🌡️ Lead morno - nutrição")
            else:
                lead_data["status"] = "cold"
                final_status = "cold"
                print(f"❄️ Lead frio - qualificação")
        
        # Debug do status final
        print(f"📋 STATUS FINAL CONFIRMADO: {final_status}")
        
        # 5. Gerar resposta baseada no STATUS FINAL (não no is_hot_lead)
        conversation_history = AutomationEngine._get_conversation_history(normalized_phone)
        
        if final_status == "qualified":
            # Lead qualificado - resposta de vendas com contexto
            if current_status != "qualified":
                # Novo lead qualificado - notificar vendas
                await IntegrationService.notify_sales_team(lead_data)
                print(f"🚨 Notificação de vendas enviada para novo lead qualificado")
            else:
                print(f"🔄 Lead já qualificado - sem nova notificação")
            
            bot_response = await AIService.generate_response(data["message"], lead_data, conversation_history)
            print(f"💬 Resposta de VENDAS gerada (lead qualificado)")
            
        elif final_status == "warm":
            # Lead morno - nutrição
            bot_response = await AIService.generate_nurture_response(data["message"], lead_data, conversation_history)
            print(f"💬 Resposta de NUTRIÇÃO gerada (lead morno)")
            
        else:
            # Lead frio - qualificação
            bot_response = await AIService.generate_qualification_response(data["message"], lead_data, conversation_history)
            print(f"💬 Resposta de QUALIFICAÇÃO gerada (lead frio)")
        
        # 6. Enviar resposta e salvar
        await IntegrationService.send_whatsapp(normalized_phone, bot_response)
        
        AutomationEngine._save_conversation(normalized_phone, data["message"], False)
        AutomationEngine._save_conversation(normalized_phone, bot_response, True)
        await IntegrationService.send_to_crm(lead_data)
        
        print(f"✅ Processamento COM CONTEXTO CORRIGIDO concluído - Score final: {new_score}, Status: {final_status}")
        print("="*60)
    
    @staticmethod
    async def _handle_status_change(data: Dict):
        """Automação para mudança de status"""
        print(f"Status changed: {data}")

    @staticmethod
    def _get_lead_data(phone: str) -> Dict:
        """Busca dados do lead no banco com telefone normalizado"""
        normalized_phone = normalize_phone(phone)
        
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM leads WHERE phone = ?', (normalized_phone,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "phone": row[1],
                "name": row[2], 
                "status": row[3],
                "score": row[4],
                "source": row[5]
            }
        return {"phone": normalized_phone, "score": 0, "status": "new"}

    @staticmethod
    def _get_conversation_history(phone: str) -> List[Dict]:
        """Busca histórico de conversa com telefone normalizado"""
        normalized_phone = normalize_phone(phone)
        
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT message, is_bot FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT 10',
            (normalized_phone,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        return [{"message": row[0], "is_bot": bool(row[1])} for row in rows]

    @staticmethod
    def _save_conversation(phone: str, message: str, is_bot: bool):
        """Salva mensagem da conversa com telefone normalizado"""
        normalized_phone = normalize_phone(phone)
        
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'INSERT INTO conversations (phone, message, is_bot) VALUES (?, ?, ?)',
            (normalized_phone, message, is_bot)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _log_automation(trigger_type: str, phone: str, action: str, result: str):
        """Log das automações executadas com telefone normalizado"""
        normalized_phone = normalize_phone(phone)
        
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'INSERT INTO automation_logs (trigger_type, phone, action_taken, result) VALUES (?, ?, ?, ?)',
            (trigger_type, normalized_phone, action, result)
        )
        conn.commit()
        conn.close()

# ==================== FUNÇÃO ANALYTICS CORRIGIDA ====================
def get_analytics_data():
    """Coleta dados para analytics com correções aplicadas"""
    conn = sqlite3.connect('previdas.db')
    cursor = conn.cursor()
    
    try:
        # Total de leads únicos (sem duplicação)
        cursor.execute("SELECT COUNT(*) FROM leads")
        total_leads = cursor.fetchone()[0]
        
        # DEBUG: Verificar se ainda há duplicação
        cursor.execute("SELECT phone, name, status, score, created_at FROM leads ORDER BY created_at DESC")
        all_leads = cursor.fetchall()
        print(f"🔍 ANALYTICS - Total de {len(all_leads)} leads ÚNICOS:")
        for lead in all_leads:
            print(f"   📱 {lead[0]} | Status: {lead[2]} | Score: {lead[3]}")
        
        # Leads por status
        cursor.execute("""
            SELECT status, COUNT(*) 
            FROM leads 
            WHERE status IS NOT NULL AND status != ''
            GROUP BY status
        """)
        leads_by_status = [{"status": row[0], "count": row[1]} for row in cursor.fetchall()]
        
        # Leads qualificados (critério corrigido: >= 75)
        cursor.execute("SELECT COUNT(*) FROM leads WHERE score >= 75")
        leads_qualificados = cursor.fetchone()[0]
        
        # Leads contatados
        cursor.execute("SELECT COUNT(*) FROM leads WHERE status = 'qualified'")
        leads_contatados = cursor.fetchone()[0]
        
        # Leads convertidos (score >= 85)
        cursor.execute("SELECT COUNT(*) FROM leads WHERE score >= 85")
        leads_convertidos = cursor.fetchone()[0]
        
        # Calcular taxas CORRIGIDAS
        taxa_qualificacao = (leads_qualificados / total_leads * 100) if total_leads > 0 else 0
        taxa_contato = (leads_contatados / total_leads * 100) if total_leads > 0 else 0
        taxa_conversao_real = (leads_convertidos / total_leads * 100) if total_leads > 0 else 0
        
        # Receita estimada (ticket médio R$ 800)
        ticket_medio = 800
        receita_gerada = leads_convertidos * ticket_medio
        
        # Score médio
        cursor.execute("SELECT AVG(score) FROM leads WHERE score IS NOT NULL")
        avg_score_result = cursor.fetchone()[0]
        avg_score = round(avg_score_result, 1) if avg_score_result else 0
        
        # Hot leads (score >= 75, critério corrigido)
        cursor.execute("""
            SELECT phone, name, score, updated_at 
            FROM leads 
            WHERE score >= 75 
            ORDER BY score DESC, updated_at DESC
        """)
        hot_leads = cursor.fetchall()
        hot_leads_list = []
        for lead in hot_leads:
            hot_leads_list.append({
                "phone": lead[0],
                "name": lead[1] if lead[1] else "Lead sem nome",
                "score": lead[2],
                "last_update": lead[3] if lead[3] else "N/A"
            })
        
        # Distribuição de score
        score_distribution = [
            {"categoria": "Muito Frio (0-19)", "count": 0},
            {"categoria": "Frio (20-49)", "count": 0},
            {"categoria": "Morno (50-74)", "count": 0},
            {"categoria": "Quente (75+)", "count": 0}
        ]
        
        cursor.execute("SELECT score FROM leads WHERE score IS NOT NULL")
        scores = [row[0] for row in cursor.fetchall()]
        
        for score in scores:
            if score <= 19:
                score_distribution[0]["count"] += 1
            elif score <= 49:
                score_distribution[1]["count"] += 1
            elif score <= 74:
                score_distribution[2]["count"] += 1
            else:
                score_distribution[3]["count"] += 1
        
        print(f"📊 MÉTRICAS CORRIGIDAS:")
        print(f"   Total Leads ÚNICOS: {total_leads}")
        print(f"   Qualificados (>=75): {leads_qualificados} ({taxa_qualificacao:.1f}%)")
        print(f"   Contatados: {leads_contatados} ({taxa_contato:.1f}%)")
        print(f"   Convertidos (>=85): {leads_convertidos} ({taxa_conversao_real:.1f}%)")
        print(f"   Score Médio: {avg_score}")
        print(f"   Receita: R$ {receita_gerada}")
        
        conn.close()
        
        return {
            "total_leads": total_leads,
            "taxa_qualificacao": round(taxa_qualificacao, 1),
            "leads_qualificados": leads_qualificados,
            "taxa_contato": round(taxa_contato, 1),
            "leads_contatados": leads_contatados,
            "taxa_conversao_real": round(taxa_conversao_real, 1),
            "leads_convertidos": leads_convertidos,
            "receita_gerada": receita_gerada,
            "ticket_medio": ticket_medio,
            "avg_score": avg_score,
            "leads_by_status": leads_by_status,
            "hot_leads_list": hot_leads_list,
            "score_distribution": score_distribution
        }
        
    except Exception as e:
        print(f"❌ Erro no get_analytics_data: {e}")
        conn.close()
        return {
            "total_leads": 0,
            "taxa_qualificacao": 0,
            "leads_qualificados": 0,
            "taxa_contato": 0,
            "leads_contatados": 0,
            "taxa_conversao_real": 0,
            "leads_convertidos": 0,
            "receita_gerada": 0,
            "ticket_medio": 0,
            "avg_score": 0,
            "leads_by_status": [],
            "hot_leads_list": [],
            "score_distribution": []
        }

# ==================== FUNÇÃO PARA MIGRAR DADOS EXISTENTES ====================
def migrate_existing_data():
    """Migra dados existentes normalizando telefones duplicados"""
    conn = sqlite3.connect('previdas.db')
    cursor = conn.cursor()
    
    try:
        print("🔄 Iniciando migração de dados existentes...")
        
        # 1. Buscar todos os leads
        cursor.execute("SELECT id, phone, name, status, score, source, created_at FROM leads")
        all_leads = cursor.fetchall()
        
        phone_groups = {}
        
        # 2. Agrupar por telefone normalizado
        for lead in all_leads:
            lead_id, phone, name, status, score, source, created_at = lead
            normalized = normalize_phone(phone)
            
            if normalized not in phone_groups:
                phone_groups[normalized] = []
            phone_groups[normalized].append({
                'id': lead_id,
                'phone': phone,
                'name': name,
                'status': status,
                'score': score,
                'source': source,
                'created_at': created_at
            })
        
        # 3. Processar duplicatas
        for normalized_phone, leads in phone_groups.items():
            if len(leads) > 1:
                print(f"📱 Encontradas {len(leads)} duplicatas para: {normalized_phone}")
                
                # Manter o lead com maior score
                best_lead = max(leads, key=lambda x: x['score'])
                leads_to_remove = [lead for lead in leads if lead['id'] != best_lead['id']]
                
                print(f"   ✅ Mantendo: ID {best_lead['id']} (Score: {best_lead['score']})")
                
                for lead_to_remove in leads_to_remove:
                    print(f"   🗑️ Removendo: ID {lead_to_remove['id']} (Score: {lead_to_remove['score']})")
                    
                    # Migrar conversas para o lead principal
                    cursor.execute(
                        "UPDATE conversations SET phone = ? WHERE phone = ?",
                        (normalized_phone, lead_to_remove['phone'])
                    )
                    
                    # Migrar logs de automação
                    cursor.execute(
                        "UPDATE automation_logs SET phone = ? WHERE phone = ?",
                        (normalized_phone, lead_to_remove['phone'])
                    )
                    
                    # Remover lead duplicado
                    cursor.execute("DELETE FROM leads WHERE id = ?", (lead_to_remove['id'],))
                
                # Atualizar telefone do lead principal para formato normalizado
                cursor.execute(
                    "UPDATE leads SET phone = ? WHERE id = ?",
                    (normalized_phone, best_lead['id'])
                )
        
        conn.commit()
        print("✅ Migração concluída com sucesso!")
        
        # 4. Verificar resultado
        cursor.execute("SELECT COUNT(*) FROM leads")
        total_after = cursor.fetchone()[0]
        print(f"📊 Total de leads após migração: {total_after}")
        
    except Exception as e:
        print(f"❌ Erro na migração: {e}")
        conn.rollback()
    finally:
        conn.close()

# ==================== ROTAS CORRIGIDAS ====================

@app.post("/leads/{lead_id}/delete")
async def delete_lead(lead_id: int):
    """Exclui um lead específico pelo ID"""
    conn = sqlite3.connect("previdas.db")
    cursor = conn.cursor()

    try:
        # 1. Buscar o número de telefone do lead
        cursor.execute("SELECT phone FROM leads WHERE id = ?", (lead_id,))
        result = cursor.fetchone()

        if result:
            phone = result[0]
            normalized_phone = normalize_phone(phone)

            # 2. Apagar conversas relacionadas
            cursor.execute("DELETE FROM conversations WHERE phone = ? OR phone = ?", (phone, normalized_phone))

            # 3. Apagar logs de automação relacionados
            cursor.execute("DELETE FROM automation_logs WHERE phone = ? OR phone = ?", (phone, normalized_phone))

            # 4. Apagar o lead
            cursor.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            
            conn.commit()
            print(f"🗑️ Lead {lead_id} removido com sucesso")

    except Exception as e:
        print(f"❌ Erro ao deletar lead: {e}")
        conn.rollback()
    finally:
        conn.close()
        
    return RedirectResponse(url="/leads", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard principal com métricas corrigidas"""
    
    # Buscar dados analytics corrigidos
    analytics = get_analytics_data()
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        **analytics
    })

@app.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request, status: str = None, search: str = None):
    """Página de gestão de leads"""
    
    conn = sqlite3.connect('previdas.db')
    
    # Query base
    query = """
        SELECT phone, name, status, score, source,
               datetime(created_at, 'localtime') as created_at,
               datetime(updated_at, 'localtime') as updated_at
        FROM leads 
        WHERE 1=1
    """
    params = []
    
    # Filtros
    if status:
        query += " AND status = ?"
        params.append(status)
    
    if search:
        query += " AND (name LIKE ? OR phone LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    
    query += " ORDER BY updated_at DESC LIMIT 50"
    
    leads = pd.read_sql_query(query, conn, params=params).to_dict('records')
    
    # Status únicos para filtro
    statuses = pd.read_sql_query(
        "SELECT DISTINCT status FROM leads ORDER BY status", conn
    )['status'].tolist()
    
    conn.close()
    
    return templates.TemplateResponse("leads.html", {
        "request": request,
        "leads": leads,
        "statuses": statuses,
        "current_status": status,
        "current_search": search or ""
    })

@app.get("/lead/{phone}", response_class=HTMLResponse)
async def lead_detail(request: Request, phone: str):
    """Detalhes de um lead específico"""
    
    normalized_phone = normalize_phone(phone)
    
    conn = sqlite3.connect('previdas.db')
    
    # Dados do lead
    lead_data = pd.read_sql_query(
        "SELECT * FROM leads WHERE phone = ?", conn, params=[normalized_phone]
    ).to_dict('records')
    
    if not lead_data:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    
    lead = lead_data[0]
    
    # Histórico de conversas
    conversations = pd.read_sql_query("""
        SELECT message, is_bot, datetime(timestamp, 'localtime') as timestamp
        FROM conversations 
        WHERE phone = ? 
        ORDER BY timestamp ASC
    """, conn, params=[normalized_phone]).to_dict('records')
    
    # Logs de automação
    automation_logs = pd.read_sql_query("""
        SELECT trigger_type, action_taken, result,
               datetime(timestamp, 'localtime') as timestamp
        FROM automation_logs 
        WHERE phone = ? 
        ORDER BY timestamp DESC
    """, conn, params=[normalized_phone]).to_dict('records')
    
    conn.close()
    
    return templates.TemplateResponse("lead_detail.html", {
        "request": request,
        "lead": lead,
        "conversations": conversations,
        "automation_logs": automation_logs
    })

@app.post("/send-message")
async def send_message_form(request: Request, phone: str = Form(...), message: str = Form(...)):
    """Enviar mensagem via formulário"""
    
    # Normalizar telefone
    normalized_phone = normalize_phone(phone)
    
    # Simular envio de mensagem
    trigger = AutomationTrigger(
        trigger_type="message_received",
        data={"phone": normalized_phone, "message": message}
    )
    
    await AutomationEngine.process_automation(trigger)
    
    return {"status": "success", "message": "Mensagem processada"}

# ==================== API ENDPOINTS CORRIGIDOS ====================

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events - substitui @app.on_event"""
    # Startup
    init_db()
    migrate_existing_data()
    
    print("🚀 Previdas Automation Engine FINAL CORRIGIDO iniciado!")
    print("✅ Problemas resolvidos:")
    print("   - Duplicação de telefones eliminada")
    print("   - IA com prompts específicos para Previdas")
    print("   - Scoring otimizado para produtos específicos")
    print("   - Decay reduzido para mensagens relevantes")
    print("   - Critérios de qualificação ajustados (>=75)")
    print("   - CONTEXTO HISTÓRICO implementado")
    print("   - Respostas contextuais para leads conhecidos")
    print("   - BUG de contexto histórico CORRIGIDO")
    
    yield
    
    # Shutdown (se necessário)
    pass

# Configurar lifespan
app.router.lifespan_context = lifespan

@app.get("/api/")
async def root():
    return {"message": "Previdas Automation Engine FINAL - Sistema inteligente com contexto histórico CORRIGIDO!"}

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(data: Dict, background_tasks: BackgroundTasks):
    """Webhook do WhatsApp com normalização de telefone"""
    
    try:
        # Extrai e normaliza dados
        phone = normalize_phone(data.get("from", ""))
        message = data.get("text", {}).get("body", "")
        
        if not phone or not message:
            raise HTTPException(status_code=400, detail="Dados inválidos")
        
        # Processa automação
        trigger = AutomationTrigger(
            trigger_type="message_received",
            data={"phone": phone, "message": message}
        )
        
        background_tasks.add_task(AutomationEngine.process_automation, trigger)
        
        return {"status": "success", "message": "Mensagem processada"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/leads")
async def create_lead(lead: Lead, background_tasks: BackgroundTasks):
    """Cria novo lead com telefone normalizado"""
    
    # Normalizar telefone
    lead.phone = normalize_phone(lead.phone)
    
    trigger = AutomationTrigger(
        trigger_type="new_lead",
        data=lead.dict()
    )
    
    background_tasks.add_task(AutomationEngine.process_automation, trigger)
    
    return {"status": "success", "message": "Lead criado e automação iniciada"}

@app.get("/api/leads/{phone}")
async def get_lead(phone: str):
    """Busca dados de um lead específico"""
    normalized_phone = normalize_phone(phone)
    lead_data = AutomationEngine._get_lead_data(normalized_phone)
    
    if not lead_data or lead_data.get("status") == "new":
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    
    return lead_data

@app.get("/api/analytics/dashboard")
async def get_dashboard_data():
    """Dados corrigidos para dashboard analytics"""
    return get_analytics_data()

@app.get("/api/conversations/{phone}")
async def get_conversation(phone: str):
    """Busca histórico de conversa com telefone normalizado"""
    normalized_phone = normalize_phone(phone)
    history = AutomationEngine._get_conversation_history(normalized_phone)
    return {"phone": normalized_phone, "conversation": history}

@app.post("/api/trigger-automation")
async def manual_trigger(trigger: AutomationTrigger, background_tasks: BackgroundTasks):
    """Trigger manual de automação (para testes)"""
    
    # Normalizar telefone se presente
    if "phone" in trigger.data:
        trigger.data["phone"] = normalize_phone(trigger.data["phone"])
    
    background_tasks.add_task(AutomationEngine.process_automation, trigger)
    
    return {"status": "success", "message": "Automação disparada"}

# ==================== NOVA ROTA PARA TESTAR CONTEXTO ====================
@app.post("/api/test-context")
async def test_context():
    """Endpoint para testar a correção de contexto histórico"""
    
    # Simular cenário do problema: lead qualificado que faz pergunta fora do escopo
    test_scenario = {
        "phone": "619255082",
        "previous_status": "qualified",
        "previous_score": 85,
        "current_message": "quero um seguro como faco"
    }
    
    # Simular processamento
    normalized_phone = normalize_phone(test_scenario["phone"])
    
    # Buscar lead atual
    lead_data = AutomationEngine._get_lead_data(normalized_phone)
    
    # Simular análise
    analysis = await AIService.analyze_message(test_scenario["current_message"])
    
    # Verificar se contexto histórico seria mantido
    would_maintain_qualification = (
        lead_data.get("status") == "qualified" and 
        analysis["score"] >= 20
    )
    
    return {
        "status": "success",
        "test_scenario": test_scenario,
        "current_lead_data": lead_data,
        "ai_analysis": analysis,
        "would_maintain_qualification": would_maintain_qualification,
        "expected_response": "Resposta contextual sobre laudos médicos",
        "fix_applied": "✅ Contexto histórico CORRIGIDO - Lead mantém status qualified"
    }

@app.post("/api/test-bug-fix")
async def test_bug_fix():
    """Endpoint específico para testar se o bug do contexto foi corrigido"""
    
    test_cases = [
        {
            "case": "Lead qualificado pergunta sobre seguro",
            "phone": "619255082",
            "message": "quero um seguro como faco",
            "expected_status": "qualified",
            "expected_response_type": "vendas_contextual"
        },
        {
            "case": "Lead qualificado pergunta sobre banco",
            "phone": "60642744499", 
            "message": "vocês fazem empréstimo?",
            "expected_status": "qualified",
            "expected_response_type": "vendas_contextual"
        }
    ]
    
    results = []
    
    for test in test_cases:
        # Buscar dados atuais do lead
        lead_data = AutomationEngine._get_lead_data(test["phone"])
        
        # Simular análise da mensagem
        analysis = await AIService.analyze_message(test["message"])
        
        # Verificar se a lógica manteria o status
        already_qualified = lead_data.get("status") == "qualified"
        ai_score = analysis["score"]
        
        would_maintain = already_qualified and ai_score >= 20
        
        results.append({
            "case": test["case"],
            "phone": test["phone"],
            "current_status": lead_data.get("status"),
            "current_score": lead_data.get("score"),
            "ai_score": ai_score,
            "would_maintain_qualified": would_maintain,
            "expected_status": test["expected_status"],
            "test_passed": would_maintain and lead_data.get("status") == "qualified"
        })
    
    all_tests_passed = all(result["test_passed"] for result in results)
    
    return {
        "status": "success" if all_tests_passed else "some_failures",
        "bug_fixed": all_tests_passed,
        "test_results": results,
        "summary": f"✅ Bug corrigido!" if all_tests_passed else "❌ Ainda há problemas"
    }

# ==================== EXECUTAR ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)