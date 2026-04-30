<?php

namespace App\Queue;

use PDO;

/**
 * Request Queue Manager
 * Gerencia fila de requisições para controlar concorrência e evitar sobrecarga no Node.js
 */
class RequestQueue
{
    private PDO $db;
    private int $maxConcurrent;
    private int $maxQueueSize;

    public function __construct(PDO $db, int $maxConcurrent = 5, int $maxQueueSize = 100)
    {
        $this->db = $db;
        $this->maxConcurrent = $maxConcurrent;
        $this->maxQueueSize = $maxQueueSize;
        $this->initializeTable();
    }

    private function initializeTable(): void
    {
        $this->db->exec("
            CREATE TABLE IF NOT EXISTS request_queue (
                id INT AUTO_INCREMENT PRIMARY KEY,
                api_key VARCHAR(255) NOT NULL,
                payload TEXT NOT NULL,
                status ENUM('pending', 'processing', 'completed', 'failed') DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL,
                result TEXT,
                error_message TEXT,
                retry_count INT DEFAULT 0,
                INDEX idx_status (status),
                INDEX idx_created (created_at)
            )
        ");
    }

    /**
     * Adiciona requisição à fila
     */
    public function enqueue(string $apiKey, array $payload): int
    {
        // Verifica tamanho da fila
        $stmt = $this->db->prepare("SELECT COUNT(*) FROM request_queue WHERE status IN ('pending', 'processing')");
        $stmt->execute();
        $currentSize = (int) $stmt->fetchColumn();

        if ($currentSize >= $this->maxQueueSize) {
            throw new \Exception('Queue is full. Try again later.', 503);
        }

        $stmt = $this->db->prepare("INSERT INTO request_queue (api_key, payload) VALUES (?, ?)");
        $stmt->execute([$apiKey, json_encode($payload)]);
        
        return (int) $this->db->lastInsertId();
    }

    /**
     * Processa próxima requisição da fila (chamado pelo worker)
     */
    public function dequeue(): ?array
    {
        try {
            $this->db->beginTransaction();

            // Seleciona próxima requisição pendente com lock
            $stmt = $this->db->prepare("
                SELECT id, api_key, payload 
                FROM request_queue 
                WHERE status = 'pending' 
                ORDER BY created_at ASC 
                LIMIT 1 
                FOR UPDATE SKIP LOCKED
            ");
            $stmt->execute();
            $row = $stmt->fetch(\PDO::FETCH_ASSOC);

            if (!$row) {
                $this->db->rollBack();
                return null;
            }

            // Marca como processando
            $updateStmt = $this->db->prepare("
                UPDATE request_queue 
                SET status = 'processing', started_at = NOW() 
                WHERE id = ?
            ");
            $updateStmt->execute([$row['id']]);

            $this->db->commit();

            return [
                'id' => $row['id'],
                'api_key' => $row['api_key'],
                'payload' => json_decode($row['payload'], true)
            ];
        } catch (\Exception $e) {
            $this->db->rollBack();
            throw $e;
        }
    }

    /**
     * Atualiza status da requisição
     */
    public function complete(int $queueId, array $result): void
    {
        $stmt = $this->db->prepare("
            UPDATE request_queue 
            SET status = 'completed', completed_at = NOW(), result = ? 
            WHERE id = ?
        ");
        $stmt->execute([json_encode($result), $queueId]);
    }

    /**
     * Marca requisição como falha
     */
    public function fail(int $queueId, string $errorMessage, bool $retry = true): void
    {
        if ($retry) {
            // Incrementa contador de retries
            $stmt = $this->db->prepare("
                UPDATE request_queue 
                SET status = 'pending', retry_count = retry_count + 1, error_message = ? 
                WHERE id = ? AND retry_count < 3
            ");
            $stmt->execute([$errorMessage, $queueId]);
        } else {
            $stmt = $this->db->prepare("
                UPDATE request_queue 
                SET status = 'failed', completed_at = NOW(), error_message = ? 
                WHERE id = ?
            ");
            $stmt->execute([$errorMessage, $queueId]);
        }
    }

    /**
     * Obtém status da requisição
     */
    public function getStatus(int $queueId): ?array
    {
        $stmt = $this->db->prepare("
            SELECT id, status, created_at, started_at, completed_at, result, error_message, retry_count 
            FROM request_queue 
            WHERE id = ?
        ");
        $stmt->execute([$queueId]);
        $row = $stmt->fetch(\PDO::FETCH_ASSOC);

        if (!$row) {
            return null;
        }

        return [
            'id' => $row['id'],
            'status' => $row['status'],
            'created_at' => $row['created_at'],
            'started_at' => $row['started_at'],
            'completed_at' => $row['completed_at'],
            'result' => $row['result'] ? json_decode($row['result'], true) : null,
            'error_message' => $row['error_message'],
            'retry_count' => $row['retry_count']
        ];
    }

    /**
     * Limpa requisições antigas completadas/falhadas
     */
    public function cleanup(int $olderThanDays = 7): int
    {
        $stmt = $this->db->prepare("
            DELETE FROM request_queue 
            WHERE status IN ('completed', 'failed') 
            AND completed_at < DATE_SUB(NOW(), INTERVAL ? DAY)
        ");
        $stmt->execute([$olderThanDays]);
        
        return $stmt->rowCount();
    }

    /**
     * Obtém estatísticas da fila
     */
    public function getStats(): array
    {
        $stats = [];
        
        $stmt = $this->db->query("SELECT status, COUNT(*) as count FROM request_queue GROUP BY status");
        while ($row = $stmt->fetch(\PDO::FETCH_ASSOC)) {
            $stats[$row['status']] = $row['count'];
        }

        $stmt = $this->db->query("SELECT AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) as avg_time FROM request_queue WHERE status = 'completed'");
        $stats['avg_processing_time'] = (float) $stmt->fetchColumn() ?: 0;

        return $stats;
    }
}
