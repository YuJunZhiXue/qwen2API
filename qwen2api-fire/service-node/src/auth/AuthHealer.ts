import { BrowserContext, Page } from 'playwright';
import axios from 'axios';

/**
 * Auto-Healing Authentication Service
 * Detecta tokens expirados e tenta renovar automaticamente
 */
export class AuthHealer {
  private readonly loginUrl = 'https://chat.qwen.ai';
  private readonly activationEmailPattern = /activation.*?code[:\s]+([A-Z0-9]{6,})/i;
  
  constructor(
    private emailService?: EmailService
  ) {}

  /**
   * Tenta renovar autenticação de conta
   */
  async healAccount(
    context: BrowserContext,
    account: { email: string; password?: string; refreshToken?: string }
  ): Promise<{ success: boolean; newToken?: string; error?: string }> {
    try {
      console.log(`[AuthHealer] Tentando renovar auth para ${account.email}`);

      // Tenta login direto se tiver senha
      if (account.password) {
        return await this.performLogin(context, account.email, account.password);
      }

      // Se tiver refresh token, tenta usar
      if (account.refreshToken) {
        const refreshed = await this.tryRefreshToken(account.refreshToken);
        if (refreshed) {
          return { success: true, newToken: refreshed };
        }
      }

      // Fallback: tenta login sem senha (pode funcionar com sessão salva)
      const page = await context.newPage();
      try {
        await page.goto(this.loginUrl, { waitUntil: 'networkidle' });
        
        // Verifica se já está logado
        const isLoggedIn = await this.checkIfLoggedIn(page);
        if (isLoggedIn) {
          const token = await this.extractToken(page);
          if (token) {
            await page.close();
            return { success: true, newToken: token };
          }
        }

        // Precisa fazer login manual
        await page.close();
        return { 
          success: false, 
          error: 'Login manual required. Password or valid session needed.' 
        };
      } finally {
        if (!page.isClosed()) {
          await page.close();
        }
      }
    } catch (error) {
      console.error('[AuthHealer] Erro ao tentar healing:', error);
      return { 
        success: false, 
        error: error instanceof Error ? error.message : 'Unknown error' 
      };
    }
  }

  /**
   * Realiza login com email/senha
   */
  private async performLogin(
    context: BrowserContext,
    email: string,
    password: string
  ): Promise<{ success: boolean; newToken?: string; error?: string }> {
    const page = await context.newPage();

    try {
      // Navega para página de login
      await page.goto(this.loginUrl, { waitUntil: 'networkidle' });
      
      // Aguarda formulário de login
      await page.waitForSelector('input[type="email"], input[name="email"]', { timeout: 10000 });

      // Preenche email
      const emailInput = page.locator('input[type="email"], input[name="email"]').first();
      await emailInput.fill(email);

      // Clica em continuar/next
      const continueButton = page.locator('button:has-text("Continuar"), button:has-text("Next"), button[type="submit"]').first();
      await continueButton.click();

      // Aguarda campo de senha
      await page.waitForSelector('input[type="password"]', { timeout: 10000 });

      // Preenche senha
      const passwordInput = page.locator('input[type="password"]').first();
      await passwordInput.fill(password);

      // Submete login
      const loginButton = page.locator('button[type="submit"], button:has-text("Entrar"), button:has-text("Sign in")').first();
      await loginButton.click();

      // Aguarda navegação ou possível código de ativação
      try {
        await page.waitForURL(/\/chat/, { timeout: 15000 });
      } catch (e) {
        // Pode precisar de código de ativação por email
        console.log('[AuthHealer] Possível necessidade de código de ativação');
        
        if (this.emailService) {
          const code = await this.emailService.getActivationCode(email);
          if (code) {
            // Tenta inserir código
            const codeInput = page.locator('input[placeholder*="code"], input[name="code"]').first();
            await codeInput.fill(code);
            
            const submitCodeButton = page.locator('button:has-text("Verify"), button:has-text("Confirmar")').first();
            await submitCodeButton.click();
            
            await page.waitForURL(/\/chat/, { timeout: 10000 });
          }
        }
      }

      // Extrai token após login bem-sucedido
      const token = await this.extractToken(page);
      
      if (token) {
        return { success: true, newToken: token };
      } else {
        return { success: false, error: 'Failed to extract token after login' };
      }
    } catch (error) {
      console.error('[AuthHealer] Erro no login:', error);
      return { 
        success: false, 
        error: error instanceof Error ? error.message : 'Login failed' 
      };
    } finally {
      if (!page.isClosed()) {
        await page.close();
      }
    }
  }

  /**
   * Tenta refresh de token via API
   */
  private async tryRefreshToken(refreshToken: string): Promise<string | null> {
    try {
      const response = await axios.post(
        'https://chat.qwen.ai/api/auth/refresh',
        { refreshToken },
        {
          headers: {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
          },
          timeout: 5000
        }
      );

      if (response.data?.accessToken) {
        return response.data.accessToken;
      }
      
      return null;
    } catch (error) {
      console.log('[AuthHealer] Refresh token falhou:', (error as any).message);
      return null;
    }
  }

  /**
   * Verifica se usuário está logado
   */
  private async checkIfLoggedIn(page: Page): Promise<boolean> {
    try {
      // Verifica elementos que só aparecem quando logado
      const chatInput = page.locator('textarea[placeholder*="Message"], textarea[placeholder*="Digite"]');
      await chatInput.waitFor({ state: 'visible', timeout: 3000 });
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Extrai token de autenticação da página
   */
  private async extractToken(page: Page): Promise<string | null> {
    try {
      const token = await page.evaluate(() => {
        // Tenta pegar de localStorage
        const localToken = localStorage.getItem('access_token') || 
                          localStorage.getItem('token') ||
                          localStorage.getItem('auth_token');
        
        if (localToken) return localToken;

        // Tenta pegar de cookies
        const cookies = document.cookie.split(';');
        for (const cookie of cookies) {
          const [name, value] = cookie.trim().split('=');
          if (name.toLowerCase().includes('token') || name.toLowerCase().includes('auth')) {
            return decodeURIComponent(value);
          }
        }

        return null;
      });

      return token;
    } catch (error) {
      console.error('[AuthHealer] Erro ao extrair token:', error);
      return null;
    }
  }

  /**
   * Detecta se erro é de autenticação inválida
   */
  isAuthError(error: any): boolean {
    const errorMsg = JSON.stringify(error).toLowerCase();
    const authIndicators = [
      'unauthorized',
      'authentication required',
      'invalid token',
      'token expired',
      '401',
      'access denied',
      'login required'
    ];

    return authIndicators.some(indicator => errorMsg.includes(indicator));
  }
}

/**
 * Serviço simples de email para pegar códigos de ativação
 * Em produção, integrar com API real de email (Gmail, SendGrid, etc)
 */
class EmailService {
  private activationCodes: Map<string, { code: string; timestamp: number }> = new Map();

  /**
   * Simula recebimento de código (em produção, viria de webhook/API)
   */
  storeActivationCode(email: string, code: string): void {
    this.activationCodes.set(email.toLowerCase(), {
      code,
      timestamp: Date.now()
    });
  }

  /**
   * Pega código de ativação mais recente
   */
  async getActivationCode(email: string): Promise<string | null> {
    const entry = this.activationCodes.get(email.toLowerCase());
    
    if (!entry) {
      return null;
    }

    // Código expira em 10 minutos
    if (Date.now() - entry.timestamp > 10 * 60 * 1000) {
      this.activationCodes.delete(email.toLowerCase());
      return null;
    }

    return entry.code;
  }
}
