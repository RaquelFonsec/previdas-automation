# main.py
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List
import openai
import requests
import json
import asyncio
from datetime import datetime
import sqlite3
import pandas as pd
from enum import Enum

app = FastAPI(title="Previdas Automation Engine", version="1.0.0")

# CORS para permitir requisições do frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== CONFIGURAÇÕES ====================
OPENAI_API_KEY = "chave_openai"
openai.api_key = OPENAI_API_KEY

# URLs dos sistemas exemplos
CRM_API_URL = "https://api.seu-crm.com"
WHATSAPP_API_URL = "https://api.whatsapp.business"
EMAIL_API_URL = "https://api.activecampaign.com"

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

# ==================== BANCO DE DADOS ====================
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

# ==================== SERVIÇOS DE IA ====================
class AIService:
    @staticmethod
    async def analyze_message(message: str, context: Dict = None) -> Dict:
        """Analisa mensagem usando GPT para classificar intenção e urgência"""
        
        prompt = f"""
        Você é um especialista em qualificação de leads para uma empresa de seguros.
        
        Analise esta mensagem e retorne um JSON com:
        - intent: "interest", "price_inquiry", "objection", "support", "casual"
        - urgency: "high", "medium", "low"
        - score: número de 0-100 (quanto maior, mais qualificado)
        - next_action: "transfer_sales", "nurture", "collect_info", "schedule_call"
        - sentiment: "positive", "neutral", "negative"
        
        Contexto do lead: {context or "Novo lead"}
        
        Mensagem: "{message}"
        
        Responda APENAS com JSON válido:
        """
        
        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200
            )
            
            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            # Fallback simples
            return {
                "intent": "interest",
                "urgency": "medium", 
                "score": 50,
                "next_action": "collect_info",
                "sentiment": "neutral"
            }

    @staticmethod
    async def generate_response(message: str, lead_data: Dict, conversation_history: List) -> str:
        """Gera resposta personalizada do chatbot"""
        
        history_text = "\n".join([
            f"{'Bot' if msg['is_bot'] else 'Cliente'}: {msg['message']}" 
            for msg in conversation_history[-5:]  # Últimas 5 mensagens
        ])
        
        prompt = f"""
        Você é um consultor especialista em seguros da Previdas.
        
        Perfil do cliente:
        - Nome: {lead_data.get('name', 'Cliente')}
        - Status: {lead_data.get('status', 'novo')}
        - Score: {lead_data.get('score', 0)}
        
        Histórico recente:
        {history_text}
        
        Mensagem atual: "{message}"
        
        Gere uma resposta que seja:
        1. Empática e personalizada
        2. Focada em entender a necessidade
        3. Direcionada para qualificar o lead
        4. Máximo 150 caracteres para WhatsApp
        
        Resposta:
        """
        
        try:
            response = await openai.ChatCompletion.acreate(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=100
            )
            
            return response.choices[0].message.content.strip()
        except Exception as e:
            return "Obrigado pelo contato! Em que posso ajudá-lo com seguros hoje?"

# ==================== INTEGRAÇÕES ====================
class IntegrationService:
    @staticmethod
    async def send_to_crm(lead_data: Dict) -> bool:
        """Envia/atualiza lead no CRM"""
        try:
            # Simula integração com CRM (HubSpot, Pipedrive, etc.)
            headers = {"Authorization": "Bearer SEU_TOKEN_CRM"}
            
            payload = {
                "phone": lead_data["phone"],
                "name": lead_data.get("name"),
                "status": lead_data["status"],
                "score": lead_data["score"],
                "source": lead_data.get("source", "whatsapp")
            }
            
            # response = requests.post(f"{CRM_API_URL}/contacts", json=payload, headers=headers)
            # return response.status_code == 200
            
            # Para demonstração, salvamos no SQLite
            conn = sqlite3.connect('previdas.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO leads (phone, name, status, score, source)
                VALUES (?, ?, ?, ?, ?)
            ''', (payload["phone"], payload["name"], payload["status"], 
                  payload["score"], payload["source"]))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            print(f"Erro ao enviar para CRM: {e}")
            return False

    @staticmethod
    async def send_whatsapp(phone: str, message: str) -> bool:
        """Envia mensagem via WhatsApp Business API"""
        try:
            headers = {"Authorization": "Bearer SEU_TOKEN_WHATSAPP"}
            
            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "text": {"body": message}
            }
            
            # response = requests.post(f"{WHATSAPP_API_URL}/messages", json=payload, headers=headers)
            # return response.status_code == 200
            
            # Para demonstração, apenas logamos
            print(f"WhatsApp para {phone}: {message}")
            return True
            
        except Exception as e:
            print(f"Erro ao enviar WhatsApp: {e}")
            return False

    @staticmethod
    async def trigger_email_sequence(email: str, sequence_type: str) -> bool:
        """Dispara sequência de email marketing"""
        try:
            headers = {"Api-Token": "SEU_TOKEN_EMAIL"}
            
            payload = {
                "contact": {"email": email},
                "automation": sequence_type  # "nurture", "onboarding", etc.
            }
            
            # response = requests.post(f"{EMAIL_API_URL}/automations", json=payload, headers=headers)
            print(f"Email {sequence_type} para {email}")
            return True
            
        except Exception as e:
            print(f"Erro ao disparar email: {e}")
            return False

    @staticmethod
    async def notify_sales_team(lead_data: Dict) -> bool:
        """Notifica equipe de vendas sobre lead quente"""
        try:
            # Integração com Slack, Teams, etc.
            message = f"""
            🔥 LEAD QUENTE!
            Nome: {lead_data.get('name', 'N/A')}
            Phone: {lead_data['phone']}
            Score: {lead_data['score']}
            Última mensagem: {lead_data.get('last_message', 'N/A')}
            """
            
            # Aqui você integraria com Slack, Teams, etc.
            print(f"Notificação vendas: {message}")
            return True
            
        except Exception as e:
            print(f"Erro ao notificar vendas: {e}")
            return False

# ==================== ENGINE DE AUTOMAÇÃO ====================
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
        
        # 1. Salva no CRM
        await IntegrationService.send_to_crm(data)
        
        # 2. Envia mensagem de boas-vindas
        welcome_msg = f"Olá! Sou da Previdas Seguros. Vi que você tem interesse. Como posso ajudar?"
        await IntegrationService.send_whatsapp(data["phone"], welcome_msg)
        
        # 3. Log da automação
        AutomationEngine._log_automation("new_lead", data["phone"], "welcome_sent", "success")

    @staticmethod
    async def _handle_message(data: Dict):
        """Automação para nova mensagem"""
        
        # 1. Busca dados do lead
        lead_data = AutomationEngine._get_lead_data(data["phone"])
        
        # 2. Analisa mensagem com IA
        analysis = await AIService.analyze_message(data["message"], lead_data)
        
        # 3. Atualiza score do lead
        new_score = min(100, lead_data.get("score", 0) + analysis["score"] // 10)
        lead_data.update({"score": new_score})
        
        # 4. Executa ação baseada na análise
        if analysis["next_action"] == "transfer_sales" or new_score > 80:
            # Lead quente - notifica vendas
            await IntegrationService.notify_sales_team(lead_data)
            lead_data["status"] = "qualified"
            
        elif analysis["next_action"] == "nurture":
            # Lead morno - sequência de nutrição
            email = lead_data.get("email")
            if email:
                await IntegrationService.trigger_email_sequence(email, "nurture")
            
        # 5. Gera e envia resposta do bot
        conversation_history = AutomationEngine._get_conversation_history(data["phone"])
        bot_response = await AIService.generate_response(
            data["message"], lead_data, conversation_history
        )
        
        await IntegrationService.send_whatsapp(data["phone"], bot_response)
        
        # 6. Salva conversa e atualiza lead
        AutomationEngine._save_conversation(data["phone"], data["message"], False)
        AutomationEngine._save_conversation(data["phone"], bot_response, True)
        await IntegrationService.send_to_crm(lead_data)

    @staticmethod
    def _get_lead_data(phone: str) -> Dict:
        """Busca dados do lead no banco"""
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM leads WHERE phone = ?', (phone,))
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
        return {"phone": phone, "score": 0, "status": "new"}

    @staticmethod
    def _get_conversation_history(phone: str) -> List[Dict]:
        """Busca histórico de conversa"""
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT message, is_bot FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT 10',
            (phone,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        return [{"message": row[0], "is_bot": bool(row[1])} for row in rows]

    @staticmethod
    def _save_conversation(phone: str, message: str, is_bot: bool):
        """Salva mensagem da conversa"""
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'INSERT INTO conversations (phone, message, is_bot) VALUES (?, ?, ?)',
            (phone, message, is_bot)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _log_automation(trigger_type: str, phone: str, action: str, result: str):
        """Log das automações executadas"""
        conn = sqlite3.connect('previdas.db')
        cursor = conn.cursor()
        
        cursor.execute(
            'INSERT INTO automation_logs (trigger_type, phone, action_taken, result) VALUES (?, ?, ?, ?)',
            (trigger_type, phone, action, result)
        )
        conn.commit()
        conn.close()

# ==================== ENDPOINTS DA API ====================

@app.on_event("startup")
async def startup_event():
    """Inicializa banco de dados"""
    init_db()
    print("🚀 Previdas Automation Engine iniciado!")

@app.get("/")
async def root():
    return {"message": "Previdas Automation Engine - Sua máquina de receita inteligente!"}

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(data: Dict, background_tasks: BackgroundTasks):
    """Webhook do WhatsApp para receber mensagens"""
    
    try:
        # Extrai dados da mensagem do WhatsApp
        phone = data.get("from", "")
        message = data.get("text", {}).get("body", "")
        
        if not phone or not message:
            raise HTTPException(status_code=400, detail="Dados inválidos")
        
        # Processa automação em background
        trigger = AutomationTrigger(
            trigger_type="message_received",
            data={"phone": phone, "message": message}
        )
        
        background_tasks.add_task(AutomationEngine.process_automation, trigger)
        
        return {"status": "success", "message": "Mensagem processada"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/leads")
async def create_lead(lead: Lead, background_tasks: BackgroundTasks):
    """Cria novo lead e dispara automações"""
    
    trigger = AutomationTrigger(
        trigger_type="new_lead",
        data=lead.dict()
    )
    
    background_tasks.add_task(AutomationEngine.process_automation, trigger)
    
    return {"status": "success", "message": "Lead criado e automação iniciada"}

@app.get("/leads/{phone}")
async def get_lead(phone: str):
    """Busca dados de um lead específico"""
    lead_data = AutomationEngine._get_lead_data(phone)
    
    if not lead_data:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    
    return lead_data

@app.get("/analytics/dashboard")
async def get_dashboard_data():
    """Dados para dashboard analytics"""
    
    conn = sqlite3.connect('previdas.db')
    
    # Leads por status
    leads_by_status = pd.read_sql_query(
        "SELECT status, COUNT(*) as count FROM leads GROUP BY status", conn
    ).to_dict('records')
    
    # Conversões por dia
    daily_conversions = pd.read_sql_query("""
        SELECT DATE(created_at) as date, COUNT(*) as leads 
        FROM leads 
        WHERE created_at >= date('now', '-30 days')
        GROUP BY DATE(created_at)
        ORDER BY date
    """, conn).to_dict('records')
    
    # Score médio dos leads
    avg_score = pd.read_sql_query(
        "SELECT AVG(score) as avg_score FROM leads", conn
    ).iloc[0]['avg_score']
    
    # Automações executadas hoje
    automations_today = pd.read_sql_query("""
        SELECT COUNT(*) as count 
        FROM automation_logs 
        WHERE DATE(timestamp) = DATE('now')
    """, conn).iloc[0]['count']
    
    conn.close()
    
    return {
        "leads_by_status": leads_by_status,
        "daily_conversions": daily_conversions,
        "avg_score": round(avg_score or 0, 2),
        "automations_today": automations_today,
        "total_leads": sum(item['count'] for item in leads_by_status)
    }

@app.get("/conversations/{phone}")
async def get_conversation(phone: str):
    """Busca histórico de conversa de um lead"""
    history = AutomationEngine._get_conversation_history(phone)
    return {"phone": phone, "conversation": history}

@app.post("/trigger-automation")
async def manual_trigger(trigger: AutomationTrigger, background_tasks: BackgroundTasks):
    """Trigger manual de automação (para testes)"""
    
    background_tasks.add_task(AutomationEngine.process_automation, trigger)
    
    return {"status": "success", "message": "Automação disparada"}

# ==================== EXECUTAR ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
