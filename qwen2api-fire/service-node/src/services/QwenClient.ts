import { BrowserSession, ChatRequest, ChatResponse, ToolCall } from '../types';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export class QwenClient {
  private baseUrl: string;
  private defaultModel: string;

  constructor() {
    this.baseUrl = process.env.QWEN_BASE_URL || 'https://chat.qwen.ai';
    this.defaultModel = process.env.DEFAULT_MODEL || 'qwen3.6-plus';
  }

  /**
   * Envia mensagem para Qwen via browser e retorna resposta stream
   */
  async *chatStream(
    session: BrowserSession,
    request: ChatRequest
  ): AsyncGenerator<string> {
    const page = session.page;
    
    try {
      // Navegar para Qwen se não estiver logado
      if (!session.isLoggedIn) {
        await this.ensureLoggedIn(session);
      }

      // Construir prompt com histórico
      const prompt = this.buildPrompt(request.messages);

      // Detectar tool calls no prompt
      const hasTools = request.tools && request.tools.length > 0;

      // Enviar mensagem via JavaScript no browser
      const responsePromise = page.evaluate(async (data) => {
        // Implementar injeção de mensagem no chat do Qwen
        // Isso depende da estrutura HTML do chat.qwen.ai
        
        // Simulação - na prática precisa inspecionar o DOM do Qwen
        return new Promise<any>((resolve) => {
          // Código real seria algo como:
          // 1. Clicar no textarea
          // 2. Digitar mensagem
          // 3. Clicar em enviar
          // 4. Observar SSE stream
          // 5. Parsear resposta
          
          resolve({ content: 'Response from Qwen', toolCalls: [] });
        });
      }, { prompt, hasTools, tools: request.tools });

      // Aguardar resposta
      const result = await responsePromise;

      // Formatar resposta OpenAI compatible
      const response: ChatResponse = {
        id: `chatcmpl-${Date.now()}`,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model: request.model || this.defaultModel,
        choices: [{
          index: 0,
          message: {
            role: 'assistant',
            content: result.content,
            tool_calls: result.toolCalls?.length > 0 ? result.toolCalls : undefined,
          },
          finish_reason: result.toolCalls?.length > 0 ? 'tool_calls' : 'stop',
        }],
        usage: {
          prompt_tokens: this.countTokens(prompt),
          completion_tokens: this.countTokens(result.content),
          total_tokens: this.countTokens(prompt) + this.countTokens(result.content),
        },
      };

      // Stream response
      if (request.stream) {
        yield `data: ${JSON.stringify(response)}\n\n`;
        yield 'data: [DONE]\n\n';
      } else {
        yield JSON.stringify(response);
      }

    } catch (error) {
      logger.error({ error }, 'Error in Qwen chat');
      
      const errorResponse = {
        error: {
          message: error instanceof Error ? error.message : 'Unknown error',
          type: 'qwen_error',
        }
      };
      
      yield `data: ${JSON.stringify(errorResponse)}\n\n`;
    }
  }

  /**
   * Garante que browser está logado no Qwen
   */
  private async ensureLoggedIn(session: BrowserSession): Promise<void> {
    const page = session.page;
    
    // Verificar se já está logado
    const isLoggedIn = await page.evaluate(() => {
      // Check for auth indicators in DOM
      return document.cookie.includes('access_token') || 
             !!document.querySelector('[data-testid="user-profile"]');
    });

    if (isLoggedIn) {
      session.isLoggedIn = true;
      logger.info('Session already logged in');
      return;
    }

    // Realizar login
    logger.info('Performing login to Qwen');
    
    await page.goto(this.baseUrl, { waitUntil: 'networkidle' });
    
    // Implementar fluxo de login real
    // 1. Clicar em login
    // 2. Preencher email/senha ou usar OAuth
    // 3. Aguardar redirecionamento
    
    session.isLoggedIn = true;
  }

  /**
   * Constrói prompt a partir do histórico de mensagens
   */
  private buildPrompt(messages: ChatRequest['messages']): string {
    return messages.map(m => {
      const role = m.role === 'system' ? 'System:' : 
                   m.role === 'user' ? 'User:' : 'Assistant:';
      return `${role} ${m.content}`;
    }).join('\n\n');
  }

  /**
   * Contagem simples de tokens (aproximada)
   */
  private countTokens(text: string): number {
    // Estimativa: ~4 caracteres por token
    return Math.ceil(text.length / 4);
  }

  /**
   * Parseia tool calls da resposta do Qwen
   */
  parseToolCalls(content: string): ToolCall[] {
    const toolCalls: ToolCall[] = [];
    
    // Procurar padrão ##TOOL_CALL## ou similar
    const toolPattern = /##TOOL_CALL##\s*({.*?})\s*##END_TOOL##/gs;
    let match;
    
    while ((match = toolPattern.exec(content)) !== null) {
      try {
        const toolData = JSON.parse(match[1]);
        toolCalls.push({
          id: `call_${Date.now()}_${toolCalls.length}`,
          type: 'function',
          function: {
            name: toolData.name,
            arguments: JSON.stringify(toolData.arguments || {}),
          },
        });
      } catch (e) {
        logger.warn({ error: e, raw: match[1] }, 'Failed to parse tool call');
      }
    }
    
    return toolCalls;
  }
}
