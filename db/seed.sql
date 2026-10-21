-- ═══ 种子数据：运动注册表（运动 = 数据行，非枚举；新增运动只需 INSERT 一行） ═══

INSERT INTO sport_registry
(sport_id, display_name, family, role, load_path, pattern, priority,
 goal_text, goal_date, weekly_sessions_min, weekly_sessions_max,
 maintenance_floor_sessions, target_load_share, mesocycle_state, obs_state, active, created_at)
VALUES
('run',         '跑步',   'run_family',   'primary',   'srpe+hr_cross', 'lower_impact',      1, NULL, NULL, 3, 6, 3, 0.35, 'progression', 'active', 1, datetime('now')),
('climb',       '攀岩',   'climb_family', 'secondary', 'srpe',          'upper_pull_finger', 2, NULL, NULL, 2, 4, 2, 0.30, 'maintenance', 'active', 1, datetime('now')),
('table_tennis','乒乓球', 'skill_fixed',  'fixed',     'srpe',          'skill_technical',   3, NULL, NULL, 1, 2, 1, 0.10, 'maintenance', 'active', 1, datetime('now')),
('strength',    '力量',   'strength',     'support',   'srpe',          'exercise_level',    2, NULL, NULL, 1, 3, 1, 0.20, 'maintenance', 'active', 1, datetime('now')),
('other',       '其他',   'unclassified', NULL,       'srpe',          'unclassified',      3, NULL, NULL, 0, 0, 0, 0.05, 'maintenance', 'observation', 1, datetime('now'));
