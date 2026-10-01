// ★ 导入顺序不能改：
//   element-plus 先，我们的 token 后 —— 两边都在 :root 上定义 --el-* 变量，
//   优先级相同，靠"后者胜"来覆盖 EP 的默认色。顺序反了主题就不生效。
import 'element-plus/dist/index.css'
import './assets/tokens.css'
import './assets/base.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
// 全量引入 Element Plus；后续按需引入可改为 unplugin-vue-components
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
