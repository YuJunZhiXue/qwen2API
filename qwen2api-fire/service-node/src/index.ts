import Fastify from 'fastify';
import cors from '@fastify/cors';
import pino from 'pino';
import { BrowserPool } from './services/BrowserPool';
import { QwenClient } from './services/QwenClient';
import { ChatRequest } from './types';

const logger = pino({ 
  level: process.env.LOG_LEVEL || 'info',
  transport: {
    target: 'pino-pretty',
    options: {
      translateTime: 'HH:MM:ss Z',
      ignore: 'pid,hostname',
    },
  },
});

async function buildServer() {
  const fastify = Fastify({
    logger: true,
    maxParamLength: 5000,
  });

  // CORS - permitir apenas backend PHP
  await fastify.register(cors, {
    origin: process.env.PHP_BACKEND_URL || '*',
    methods: ['GET', 'POST', 'OPTIONS'],
  });

  // Middleware de autenticação
  fastify.addHook('preHandler', async (request, reply) => {
    const apiKey = request.headers['x-api-key'];
    const expectedKey = process.env.NODE_API_KEY || 'secret-key-change-in-production';

    if (apiKey !== expectedKey) {
      reply.code(401).send({ error: 'Invalid API key' });
    }
  });

  // Health check
  fastify.get('/health', async (request, reply) => {
    return {
      status: 'ok',
      timestamp: new Date().toISOString(),
      uptime: process.uptime(),
    };
  });

  // Chat completions endpoint
  fastify.post('/chat/completions', async (request, reply) => {
    const body = request.body as ChatRequest;

    // Validar payload
    if (!body.messages || !Array.isArray(body.messages)) {
      return reply.code(400).send({ 
        error: { message: 'Messages array is required', type: 'bad_request' } 
      });
    }

    // Configurar streaming se solicitado
    if (body.stream) {
      reply.header('Content-Type', 'text/event-stream');
      reply.header('Cache-Control', 'no-cache');
      reply.header('Connection', 'keep-alive');

      const browserPool = new BrowserPool();
      const qwenClient = new QwenClient();

      try {
        // Adquirir sessão do pool
        const session = await browserPool.acquireSession();

        // Stream resposta do Qwen para o cliente
        for await (const chunk of qwenClient.chatStream(session, body)) {
          reply.raw.write(chunk);
        }

        // Liberar sessão
        browserPool.releaseSession(session.id);
        reply.raw.end();

      } catch (error) {
        logger.error({ error }, 'Chat streaming error');
        reply.raw.write(`data: ${JSON.stringify({ error: { message: 'Streaming failed' } })}\n\n`);
        reply.raw.end();
      }
    } else {
      // Resposta síncrona
      const browserPool = new BrowserPool();
      const qwenClient = new QwenClient();

      try {
        const session = await browserPool.acquireSession();
        
        let fullResponse = '';
        for await (const chunk of qwenClient.chatStream(session, body)) {
          fullResponse += chunk;
        }

        browserPool.releaseSession(session.id);

        // Parsear resposta final
        const response = JSON.parse(fullResponse);
        return reply.send(response);

      } catch (error) {
        logger.error({ error }, 'Chat sync error');
        return reply.code(500).send({ 
          error: { message: 'Request failed', type: 'server_error' } 
        });
      }
    }
  });

  // Account management endpoint
  fastify.post('/accounts/manage', async (request, reply) => {
    const { action, data } = request.body as { action: string; data: any };

    // Implementar gestão de contas (login, refresh token, etc)
    logger.info({ action, data }, 'Account management request');

    return reply.send({ 
      success: true, 
      message: `Action ${action} completed` 
    });
  });

  // Error handler global
  fastify.setErrorHandler((error, request, reply) => {
    logger.error({ error }, 'Unhandled error');
    
    reply.code(500).send({
      error: {
        message: error.message,
        type: 'internal_error',
      },
    });
  });

  return fastify;
}

// Start server
const start = async () => {
  const fastify = await buildServer();
  
  const port = parseInt(process.env.PORT || '3000');
  const host = '0.0.0.0';

  try {
    await fastify.listen({ port, host });
    logger.info(`🚀 Qwen2API Fire Node Service running on http://${host}:${port}`);
  } catch (err) {
    logger.error(err);
    process.exit(1);
  }

  // Graceful shutdown
  const signals = ['SIGINT', 'SIGTERM'];
  signals.forEach(signal => {
    process.on(signal, async () => {
      logger.info(`Shutting down gracefully...`);
      await fastify.close();
      process.exit(0);
    });
  });
};

start();
