<?php

require __DIR__ . '/../vendor/autoload.php';

use Slim\Factory\AppFactory;
use DI\Container;
use App\Services\NodeService;
use App\Services\QuotaService;
use App\Controllers\ChatController;

// Configurar container DI
$container = new Container();

// Configurar PDO
$container->set('db', function () {
    $dsn = getenv('DB_DSN') ?: 'mysql:host=localhost;dbname=qwen2api;charset=utf8mb4';
    $user = getenv('DB_USER') ?: 'root';
    $pass = getenv('DB_PASS') ?: '';
    
    $pdo = new PDO($dsn, $user, $pass, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    
    return $pdo;
});

// Registrar serviços
$container->set(NodeService::class, function () {
    return new NodeService();
});

$container->set(QuotaService::class, function ($c) {
    return new QuotaService($c->get('db'));
});

$container->set(ChatController::class, function ($c) {
    return new ChatController(
        $c->get(NodeService::class),
        $c->get(QuotaService::class)
    );
});

AppFactory::setContainer($container);
$app = AppFactory::create();

// Middleware de CORS
$app->add(function ($request, $handler) {
    $response = $handler->handle($request);
    return $response
        ->withHeader('Access-Control-Allow-Origin', '*')
        ->withHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        ->withHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key');
});

// Middleware de Auth (simplificado)
$app->add(function ($request, $handler) {
    $apiKey = $request->getHeaderLine('Authorization');
    
    if (strpos($apiKey, 'Bearer ') === 0) {
        $apiKey = substr($apiKey, 7);
    }
    
    // Validar API key (implementar lógica real)
    if (!empty($apiKey)) {
        $request = $request->withAttribute('api_key', $apiKey);
        $request = $request->withAttribute('user_id', 'user_' . md5($apiKey));
    }
    
    return $handler->handle($request);
});

// Rotas OpenAI Compatible
$app->post('/v1/chat/completions', [\ChatController::class, 'chatCompletions']);

// Rotas Anthropic Compatible
$app->post('/anthropic/v1/messages', function ($request, $response) {
    // Implementar similar ao OpenAI
    return $response->write(json_encode(['error' => 'Not implemented yet']));
});

// Rotas Gemini Compatible
$app->post('/v1beta/models/{model}:generateContent', function ($request, $response, $args) {
    // Implementar similar ao OpenAI
    return $response->write(json_encode(['error' => 'Not implemented yet']));
});

// Health check
$app->get('/health', function ($request, $response) {
    /** @var NodeService $nodeService */
    $nodeService = $this->get(NodeService::class);
    
    $healthy = $nodeService->healthCheck();
    
    return $response->write(json_encode([
        'status' => $healthy ? 'ok' : 'degraded',
        'node_service' => $healthy ? 'connected' : 'disconnected',
        'timestamp' => date('c'),
    ]));
});

// Models endpoint
$app->get('/v1/models', function ($request, $response) {
    $models = [
        ['id' => 'qwen3.6-plus', 'object' => 'model', 'owned_by' => 'qwen'],
        ['id' => 'gpt-4o', 'object' => 'model', 'owned_by' => 'openai'],
        ['id' => 'claude-3-5-sonnet', 'object' => 'model', 'owned_by' => 'anthropic'],
        ['id' => 'gemini-pro', 'object' => 'model', 'owned_by' => 'google'],
    ];
    
    return $response->write(json_encode(['data' => $models]));
});

// Error handler
$errorMiddleware = $app->addErrorMiddleware(true, true, true);
$errorMiddleware->setDefaultErrorHandler(function ($request, \Throwable $exception) {
    return json_encode([
        'error' => [
            'message' => $exception->getMessage(),
            'type' => 'server_error',
        ]
    ]);
});

$app->run();
