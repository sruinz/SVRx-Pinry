import { createRouter, createWebHistory } from 'vue-router';
import Home from '../views/Home.vue';

const routes = [
  {
    path: '/',
    name: 'home',
    component: Home,
  },
  {
    path: '/pins/tags/:tag',
    name: 'tag',
    component: () => import('../views/Pins4Tag.vue'),
  },
  {
    path: '/pins/users/:user',
    name: 'user',
    component: () => import('../views/Pins4User.vue'),
  },
  {
    path: '/pins/boards/:boardId',
    name: 'board',
    component: () => import('../views/Pins4Board.vue'),
  },
  {
    path: '/pins/:pinId',
    name: 'pin',
    component: () => import('../views/Pins4Id.vue'),
  },
  {
    path: '/boards/users/:username',
    name: 'boards4user',
    component: () => import('../views/Boards4User.vue'),
  },
  {
    path: '/profile/:username',
    name: 'profile4user',
    component: () => import('../views/Profile4User.vue'),
  },
  {
    path: '/pin-creation/from-url',
    name: 'pin-creation-from-url',
    component: () => import('../views/PinCreate.vue'),
  },
  {
    path: '/search',
    name: 'search',
    component: () => import('../views/Search.vue'),
  },
  {
    path: '/exports',
    name: 'exports',
    component: () => import('../views/Exports.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'PageNotFound',
    component: () => import('../views/PageNotFound.vue'),
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
