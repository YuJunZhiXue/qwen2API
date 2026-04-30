import { ToolCall, ToolDefinition } from '../types';

/**
 * Tool Call Parser
 * Detecta e parseia chamadas de função no formato ##TOOL_CALL## do Qwen
 */
export class ToolParser {
  private toolDefinitions: Map<string, ToolDefinition> = new Map();

  constructor(tools?: ToolDefinition[]) {
    if (tools) {
      tools.forEach(tool => this.registerTool(tool));
    }
  }

  /**
   * Registra uma definição de ferramenta
   */
  registerTool(tool: ToolDefinition): void {
    this.toolDefinitions.set(tool.function.name, tool);
  }

  /**
   * Detecta se há chamadas de ferramenta no texto
   */
  hasToolCalls(text: string): boolean {
    return text.includes('##TOOL_CALL##');
  }

  /**
   * Parseia chamadas de ferramenta do texto
   * Retorna array de ToolCall objetos
   */
  parseToolCalls(text: string): ToolCall[] {
    const toolCalls: ToolCall[] = [];
    
    // Regex para capturar blocos ##TOOL_CALL##
    const toolCallRegex = /##TOOL_CALL##\s*([\s\S]*?)\s*##END_TOOL_CALL##/g;
    let match;

    while ((match = toolCallRegex.exec(text)) !== null) {
      const toolBlock = match[1].trim();
      
      try {
        // Tenta parsear JSON direto
        const toolData = JSON.parse(toolBlock);
        
        if (toolData.name && typeof toolData.arguments === 'object') {
          toolCalls.push({
            id: `call_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
            type: 'function',
            function: {
              name: toolData.name,
              arguments: JSON.stringify(toolData.arguments)
            }
          });
        }
      } catch (jsonError) {
        // Se falhar JSON puro, tenta extrair com regex mais flexível
        const extracted = this.extractToolCallFlexibly(toolBlock);
        if (extracted) {
          toolCalls.push(extracted);
        }
      }
    }

    return toolCalls;
  }

  /**
   * Extração flexível para JSON mal formado
   */
  private extractToolCallFlexibly(block: string): ToolCall | null {
    // Tenta encontrar nome da função
    const nameMatch = block.match(/"name"\s*:\s*"([^"]+)"/i);
    if (!nameMatch) return null;

    const functionName = nameMatch[1];
    
    // Verifica se a função está registrada
    const toolDef = this.toolDefinitions.get(functionName);
    if (!toolDef) {
      console.warn(`Tool "${functionName}" not registered`);
    }

    // Tenta extrair argumentos
    let argsString = '{}';
    const argsMatch = block.match(/"arguments"\s*:\s*({[\s\S]*?})(?:,|"|$)/i);
    
    if (argsMatch) {
      argsString = argsMatch[1];
      
      // Tenta corrigir JSON comum problemas
      argsString = this.fixCommonJsonIssues(argsString);
    }

    try {
      const args = JSON.parse(argsString);
      return {
        id: `call_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        type: 'function',
        function: {
          name: functionName,
          arguments: JSON.stringify(args)
        }
      };
    } catch (e) {
      console.error('Failed to parse tool arguments:', e);
      return null;
    }
  }

  /**
   * Corrige problemas comuns de JSON
   */
  private fixCommonJsonIssues(json: string): string {
    let fixed = json;

    // Remove trailing commas
    fixed = fixed.replace(/,(\s*[}\]])/g, '$1');

    // Adiciona aspas em chaves não quoted
    fixed = fixed.replace(/([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:/g, '$1"$2":');

    // Converte single quotes para double quotes (cuidado com strings internas)
    // fixed = fixed.replace(/'/g, '"'); // Muito arriscado, melhor não fazer

    // Remove comentários (se houver)
    fixed = fixed.replace(/\/\/.*$/gm, '');

    return fixed;
  }

  /**
   * Extrai apenas o conteúdo de texto sem as chamadas de ferramenta
   */
  extractTextContent(text: string): string {
    return text.replace(/##TOOL_CALL##\s*[\s\S]*?\s*##END_TOOL_CALL##/g, '').trim();
  }

  /**
   * Valida se os argumentos correspondem ao schema da ferramenta
   */
  validateToolCall(toolCall: ToolCall): { valid: boolean; errors: string[] } {
    const errors: string[] = [];
    const toolDef = this.toolDefinitions.get(toolCall.function.name);

    if (!toolDef) {
      errors.push(`Tool "${toolCall.function.name}" not registered`);
      return { valid: false, errors };
    }

    try {
      const args = JSON.parse(toolCall.function.arguments);
      const schema = toolDef.function.parameters;

      // Validação básica de required fields
      if (schema?.required) {
        for (const field of schema.required) {
          if (!(field in args)) {
            errors.push(`Missing required field: ${field}`);
          }
        }
      }

      // Validação de tipos (básica)
      if (schema?.properties) {
        for (const [key, prop] of Object.entries(schema.properties)) {
          if (key in args) {
            const expectedType = (prop as any).type;
            const actualType = typeof args[key];

            if (expectedType && expectedType !== actualType) {
              errors.push(`Field "${key}" should be ${expectedType}, got ${actualType}`);
            }
          }
        }
      }
    } catch (e) {
      errors.push('Invalid JSON in arguments');
    }

    return { valid: errors.length === 0, errors };
  }

  /**
   * Formata resultado de ferramenta para enviar de volta ao modelo
   */
  formatToolResult(toolCallId: string, result: any): string {
    return JSON.stringify({
      role: 'tool',
      content: typeof result === 'string' ? result : JSON.stringify(result),
      tool_call_id: toolCallId
    });
  }

  /**
   * Formata erro de ferramenta
   */
  formatToolError(toolCallId: string, error: string): string {
    return JSON.stringify({
      role: 'tool',
      content: `Error: ${error}`,
      tool_call_id: toolCallId
    });
  }
}
