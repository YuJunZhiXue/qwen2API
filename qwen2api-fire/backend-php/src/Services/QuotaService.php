<?php

namespace App\Services;

use PDO;

/**
 * Serviço de gestão de quotas e usage dos usuários
 */
class QuotaService
{
    private PDO $db;

    public function __construct(PDO $db)
    {
        $this->db = $db;
    }

    /**
     * Verifica se usuário tem quota disponível
     */
    public function checkQuota(string $userId, string $model): array
    {
        $stmt = $this->db->prepare('
            SELECT 
                daily_limit,
                daily_used,
                monthly_limit,
                monthly_used,
                reset_daily_at,
                reset_monthly_at
            FROM user_quotas 
            WHERE user_id = ? AND model = ?
        ');
        
        $stmt->execute([$userId, $model]);
        $quota = $stmt->fetch(PDO::FETCH_ASSOC);

        if (!$quota) {
            // Cria quota padrão se não existir
            return $this->createDefaultQuota($userId, $model);
        }

        $dailyRemaining = $quota['daily_limit'] - $quota['daily_used'];
        $monthlyRemaining = $quota['monthly_limit'] - $quota['monthly_used'];

        return [
            'allowed' => $dailyRemaining > 0 && $monthlyRemaining > 0,
            'daily_remaining' => max(0, $dailyRemaining),
            'monthly_remaining' => max(0, $monthlyRemaining),
            'daily_limit' => $quota['daily_limit'],
            'monthly_limit' => $quota['monthly_limit'],
            'reset_daily_at' => $quota['reset_daily_at'],
            'reset_monthly_at' => $quota['reset_monthly_at'],
        ];
    }

    /**
     * Atualiza quota após uso
     */
    public function updateUsage(string $userId, string $model, int $tokens): void
    {
        $stmt = $this->db->prepare('
            UPDATE user_quotas 
            SET 
                daily_used = daily_used + ?,
                monthly_used = monthly_used + ?,
                updated_at = NOW()
            WHERE user_id = ? AND model = ?
        ');
        
        $stmt->execute([$tokens, $tokens, $userId, $model]);
    }

    /**
     * Registra log de requisição
     */
    public function logRequest(string $userId, string $model, int $inputTokens, int $outputTokens, string $status): void
    {
        $stmt = $this->db->prepare('
            INSERT INTO request_logs 
                (user_id, model, input_tokens, output_tokens, status, created_at)
            VALUES (?, ?, ?, ?, ?, NOW())
        ');
        
        $stmt->execute([$userId, $model, $inputTokens, $outputTokens, $status]);
    }

    /**
     * Cria quota padrão para novo usuário
     */
    private function createDefaultQuota(string $userId, string $model): array
    {
        $defaults = [
            'qwen3.6-plus' => ['daily' => 100000, 'monthly' => 2000000],
            'default' => ['daily' => 50000, 'monthly' => 1000000],
        ];

        $limits = $defaults[$model] ?? $defaults['default'];

        $stmt = $this->db->prepare('
            INSERT INTO user_quotas 
                (user_id, model, daily_limit, daily_used, monthly_limit, monthly_used, 
                 reset_daily_at, reset_monthly_at, created_at)
            VALUES (?, ?, ?, 0, ?, 0, DATE_ADD(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 30 DAY), NOW())
        ');
        
        $stmt->execute([$userId, $model, $limits['daily'], $limits['monthly']]);

        return [
            'allowed' => true,
            'daily_remaining' => $limits['daily'],
            'monthly_remaining' => $limits['monthly'],
            'daily_limit' => $limits['daily'],
            'monthly_limit' => $limits['monthly'],
            'reset_daily_at' => date('Y-m-d H:i:s', strtotime('+1 day')),
            'reset_monthly_at' => date('Y-m-d H:i:s', strtotime('+30 days')),
        ];
    }

    /**
     * Reseta quotas diárias/mensais expiradas
     */
    public function resetExpiredQuotas(): int
    {
        // Reset diário
        $stmt = $this->db->prepare('
            UPDATE user_quotas 
            SET daily_used = 0, reset_daily_at = DATE_ADD(NOW(), INTERVAL 1 DAY)
            WHERE reset_daily_at <= NOW()
        ');
        $stmt->execute();
        $dailyReset = $stmt->rowCount();

        // Reset mensal
        $stmt = $this->db->prepare('
            UPDATE user_quotas 
            SET monthly_used = 0, reset_monthly_at = DATE_ADD(NOW(), INTERVAL 30 DAY)
            WHERE reset_monthly_at <= NOW()
        ');
        $stmt->execute();
        $monthlyReset = $stmt->rowCount();

        return $dailyReset + $monthlyReset;
    }
}
