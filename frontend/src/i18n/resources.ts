// Single aggregation point for all locale resources.
// Todos 7-10 will extend this file as new namespaces/locale JSON files are added.
// Wave 1 (common/errors) + Wave 2 (chapters/characters/outline/settings) +
// Wave 2 batch3 (16 page namespaces) — registered here.
import zhCommon from '../locales/zh/common.json';
import zhErrors from '../locales/zh/errors.json';
import zhChapters from '../locales/zh/chapters.json';
import zhCharacters from '../locales/zh/characters.json';
import zhOutline from '../locales/zh/outline.json';
import zhSettings from '../locales/zh/settings.json';
import zhAuthCallback from '../locales/zh/authCallback.json';
import zhBookshelf from '../locales/zh/bookshelf.json';
import zhLogin from '../locales/zh/login.json';
import zhCareers from '../locales/zh/careers.json';
import zhChapterAnalysis from '../locales/zh/chapterAnalysis.json';
import zhChapterReader from '../locales/zh/chapterReader.json';
import zhOrganizations from '../locales/zh/organizations.json';
import zhProjectDetail from '../locales/zh/projectDetail.json';
import zhProjectWizard from '../locales/zh/projectWizard.json';
import zhPromptTemplates from '../locales/zh/promptTemplates.json';
import zhRelationships from '../locales/zh/relationships.json';
import zhSkillChat from '../locales/zh/skillChat.json';
import zhSkillManage from '../locales/zh/skillManage.json';
import zhSystemSettings from '../locales/zh/systemSettings.json';
import zhUserManagement from '../locales/zh/userManagement.json';
import zhWorldSetting from '../locales/zh/worldSetting.json';
import zhWritingStyles from '../locales/zh/writingStyles.json';
import enCommon from '../locales/en/common.json';
import enErrors from '../locales/en/errors.json';
import enChapters from '../locales/en/chapters.json';
import enCharacters from '../locales/en/characters.json';
import enOutline from '../locales/en/outline.json';
import enSettings from '../locales/en/settings.json';
import enAuthCallback from '../locales/en/authCallback.json';
import enBookshelf from '../locales/en/bookshelf.json';
import enLogin from '../locales/en/login.json';
import enCareers from '../locales/en/careers.json';
import enChapterAnalysis from '../locales/en/chapterAnalysis.json';
import enChapterReader from '../locales/en/chapterReader.json';
import enOrganizations from '../locales/en/organizations.json';
import enProjectDetail from '../locales/en/projectDetail.json';
import enProjectWizard from '../locales/en/projectWizard.json';
import enPromptTemplates from '../locales/en/promptTemplates.json';
import enRelationships from '../locales/en/relationships.json';
import enSkillChat from '../locales/en/skillChat.json';
import enSkillManage from '../locales/en/skillManage.json';
import enSystemSettings from '../locales/en/systemSettings.json';
import enUserManagement from '../locales/en/userManagement.json';
import enWorldSetting from '../locales/en/worldSetting.json';
import enWritingStyles from '../locales/en/writingStyles.json';

export const resources = {
  zh: {
    common: zhCommon,
    errors: zhErrors,
    chapters: zhChapters,
    characters: zhCharacters,
    outline: zhOutline,
    settings: zhSettings,
    authCallback: zhAuthCallback,
    bookshelf: zhBookshelf,
    login: zhLogin,
    careers: zhCareers,
    chapterAnalysis: zhChapterAnalysis,
    chapterReader: zhChapterReader,
    organizations: zhOrganizations,
    projectDetail: zhProjectDetail,
    projectWizard: zhProjectWizard,
    promptTemplates: zhPromptTemplates,
    relationships: zhRelationships,
    skillChat: zhSkillChat,
    skillManage: zhSkillManage,
    systemSettings: zhSystemSettings,
    userManagement: zhUserManagement,
    worldSetting: zhWorldSetting,
    writingStyles: zhWritingStyles,
  },
  en: {
    common: enCommon,
    errors: enErrors,
    chapters: enChapters,
    characters: enCharacters,
    outline: enOutline,
    settings: enSettings,
    authCallback: enAuthCallback,
    bookshelf: enBookshelf,
    login: enLogin,
    careers: enCareers,
    chapterAnalysis: enChapterAnalysis,
    chapterReader: enChapterReader,
    organizations: enOrganizations,
    projectDetail: enProjectDetail,
    projectWizard: enProjectWizard,
    promptTemplates: enPromptTemplates,
    relationships: enRelationships,
    skillChat: enSkillChat,
    skillManage: enSkillManage,
    systemSettings: enSystemSettings,
    userManagement: enUserManagement,
    worldSetting: enWorldSetting,
    writingStyles: enWritingStyles,
  },
} as const;
