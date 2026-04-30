<?php

namespace App\Middleware;

use Psr\Http\Message\ServerRequestInterface as Request;
use Psr\Http\Server\RequestHandlerInterface as Handler;
use Psr\Http\Message\ResponseInterface as Response;
use Slim\Psr7\Factory\ResponseFactory;

/**
 * Rate Limiting Middleware
 * Limita requisições por IP para evitar bloqueios do Qwen e abuso da API
 */
class RateLimitMiddleware
{
    private array $limits = [];
    private int $windowSeconds;
    private int $maxRequests;
    private \PDO $db;

    public function __construct(\PDO $db, int $maxRequests = 60, int $windowSeconds = 60)
    {
        $this->db = $db;
        $this->maxRequests = $maxRequests;
        $this->windowSeconds = $windowSeconds;
    }

    public function __invoke(Request $request, Handler $handler): Response
    {
        $ip = $this->getClientIp($request);
        $apiKey = $request->getHeaderLine('Authorization');
        $key = hash('sha256', $ip . '_' . $apiKey);
        
        $now = time();
        $windowStart = $now - $this->windowSeconds;

        // Limpa registros antigos
        $stmt = $this->db->prepare("DELETE FROM rate_limits WHERE timestamp < ?");
        $stmt->execute([$windowStart]);

        // Conta requisições atuais
        $stmt = $this->db->prepare("SELECT COUNT(*) FROM rate_limits WHERE ip_hash = ? AND timestamp > ?");
        $stmt->execute([$key, $windowStart]);
        $count = (int) $stmt->fetchColumn();

        if ($count >= $this->maxRequests) {
            $responseFactory = new ResponseFactory();
            $response = $responseFactory->createResponse(429, 'Too Many Requests');
            $response->getBody()->write(json_encode([
                'error' => [
                    'message' => 'Rate limit exceeded. Try again later.',
                    'type' => 'rate_limit_error',
                    'retry_after' => $this->windowSeconds
                ]
            ]));
            return $response->withHeader('Content-Type', 'application/json')
                           ->withHeader('Retry-After', (string) $this->windowSeconds);
        }

        // Registra nova requisição
        $stmt = $this->db->prepare("INSERT INTO rate_limits (ip_hash, timestamp) VALUES (?, ?)");
        $stmt->execute([$key, $now]);

        return $handler->handle($request);
    }

    private function getClientIp(Request $request): string
    {
        if ($request->hasHeader('X-Forwarded-For')) {
            return explode(',', $request->getHeaderLine('X-Forwarded-For'))[0];
        }
        if ($request->hasHeader('X-Real-IP')) {
            return $request->getHeaderLine('X-Real-IP');
        }
        $serverParams = $request->getServerParams();
        return $serverParams['REMOTE_ADDR'] ?? '0.0.0.0';
    }
}
