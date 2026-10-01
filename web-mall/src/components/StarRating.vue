<script setup lang="ts">
/**
 * 星级。两种用法：
 *
 * - 只读展示：`<StarRating :score="4" />`
 * - 可点选择：`<StarRating v-model="score" editable />`
 *
 * 自己画而不用 `el-rate`：星星的颜色要跟着皮肤走（大促皮肤下是品牌色，
 * 默认皮肤下是警示橙），el-rate 的颜色得靠 CSS 变量覆盖，反而更绕。
 */
const props = withDefaults(
  defineProps<{
    score: number
    /** 是否可点选 */
    editable?: boolean
    /** 尺寸：sm 用在列表里，md 用在详情页 */
    size?: 'sm' | 'md' | 'lg'
    /** 只读时是否显示数字（如 "4.5"） */
    showScore?: boolean
  }>(),
  { editable: false, size: 'sm', showScore: false },
)

const emit = defineEmits<{ 'update:score': [value: number] }>()

const STARS = [1, 2, 3, 4, 5]

function pick(value: number): void {
  if (!props.editable) return
  emit('update:score', value)
}
</script>

<template>
  <span class="stars" :class="[`size-${size}`, { editable }]" role="img" :aria-label="`${score} 星`">
    <button
      v-for="s in STARS"
      :key="s"
      type="button"
      class="star"
      :class="{ on: s <= Math.round(score) }"
      :disabled="!editable"
      :tabindex="editable ? 0 : -1"
      @click="pick(s)"
    >
      ★
    </button>
    <span v-if="showScore" class="num tnum">{{ score.toFixed(1) }}</span>
  </span>
</template>

<style scoped>
.stars {
  display: inline-flex;
  align-items: center;
  gap: 1px;
  line-height: 1;
}

.star {
  padding: 0;
  border: none;
  background: none;
  color: var(--color-border-strong);
  cursor: default;
  transition: color var(--dur-fast) var(--ease-out);
}

/* 点亮的星星用警示色 —— 与"待付款"同一个语义色，表示"强调" */
.star.on {
  color: var(--color-warning);
}

.editable .star {
  cursor: pointer;
}

.editable .star:hover {
  color: var(--color-warning);
}

.size-sm .star {
  font-size: var(--text-sm);
}

.size-md .star {
  font-size: var(--text-md);
}

.size-lg .star {
  font-size: var(--text-2xl);
}

.num {
  margin-left: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
</style>
