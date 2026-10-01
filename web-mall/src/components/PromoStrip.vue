<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'

/**
 * 可换肤营销带。
 *
 * 这是"大促可换肤"最直观的落点：**结构恒定，只换皮**。
 * 中性主题下它是一条安静的灰带（`--promo-hero-bg` = 中性灰），
 * 大促主题下同一段 DOM 变成渐变横幅 + 促销徽标。
 *
 * 文案来自 `theme/themes.ts`，颜色全部来自 `--promo-*` token，
 * 所以加新皮肤时这个组件一行都不用动。
 */
const { themeDef } = useTheme()
</script>

<template>
  <section class="promo" :class="{ 'is-festive': !!themeDef.badge }">
    <div class="promo-inner">
      <span v-if="themeDef.badge" class="promo-badge">{{ themeDef.badge }}</span>
      <h2 class="promo-headline">{{ themeDef.headline }}</h2>
      <p class="promo-slogan">{{ themeDef.slogan }}</p>
    </div>
  </section>
</template>

<style scoped>
.promo {
  background: var(--promo-hero-bg);
  color: var(--promo-hero-text);
  border-bottom: 1px solid var(--color-border);
}

/* 中性主题是"静灰带"，不需要分隔线压得太重 */
.promo.is-festive {
  border-bottom-color: transparent;
}

.promo-inner {
  max-width: var(--layout-max);
  margin: 0 auto;
  padding: var(--space-4) var(--layout-gutter);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.promo-badge {
  flex: 0 0 auto;
  padding: 2px var(--space-2);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: 0.02em;
  border-radius: var(--radius-sm);
  background: var(--promo-badge-bg);
  color: var(--promo-badge-text);
}

.promo-headline {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
}

.promo-slogan {
  font-size: var(--text-sm);
  color: var(--promo-hero-text-soft);
}
</style>
