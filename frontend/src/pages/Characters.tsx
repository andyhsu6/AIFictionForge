import { useState, useEffect, useRef } from 'react';
import { Button, Modal, Form, Input, Select, message, Row, Col, Empty, Tabs, Divider, Typography, Space, InputNumber, Checkbox, theme } from 'antd';
import { ThunderboltOutlined, UserOutlined, TeamOutlined, PlusOutlined, ExportOutlined, ImportOutlined, DownloadOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import { useCharacterSync } from '../store/hooks';
import { charactersPageGridConfig } from '../components/CardStyles';
import { CharacterCard } from '../components/CharacterCard';
import { SSELoadingOverlay } from '../components/SSELoadingOverlay';
import type { Character, ApiError } from '../types';
import { characterApi } from '../services/api';
import { SSEPostClient } from '../utils/sseClient';
import api from '../services/api';
import { useTranslation } from 'react-i18next';

const { Title } = Typography;
const { TextArea } = Input;

interface Career {
  id: string;
  name: string;
  type: 'main' | 'sub';
  max_stage: number;
}

// 副职业数据类型
interface SubCareerData {
  career_id: string;
  stage: number;
}

// 角色创建表单值类型
interface CharacterFormValues {
  name: string;
  age?: string;
  gender?: string;
  role_type?: string;
  personality?: string;
  appearance?: string;
  background?: string;
  main_career_id?: string;
  main_career_stage?: number;
  sub_career_data?: SubCareerData[];
  // 组织字段
  organization_type?: string;
  organization_purpose?: string;
  organization_members?: string;
  power_level?: number;
  location?: string;
  motto?: string;
  color?: string;
}

// 角色创建数据类型
interface CharacterCreateData {
  project_id: string;
  name: string;
  is_organization: boolean;
  age?: string;
  gender?: string;
  role_type?: string;
  personality?: string;
  appearance?: string;
  background?: string;
  main_career_id?: string;
  main_career_stage?: number;
  sub_careers?: string;
  organization_type?: string;
  organization_purpose?: string;
  organization_members?: string;
  power_level?: number;
  location?: string;
  motto?: string;
  color?: string;
}

// 角色更新数据类型
interface CharacterUpdateData {
  name?: string;
  age?: string;
  gender?: string;
  role_type?: string;
  personality?: string;
  appearance?: string;
  background?: string;
  main_career_id?: string;
  main_career_stage?: number;
  sub_careers?: string;
  organization_type?: string;
  organization_purpose?: string;
  organization_members?: string;
  power_level?: number;
  location?: string;
  motto?: string;
  color?: string;
}

export default function Characters() {
  const { t } = useTranslation('characters');
  const { token } = theme.useToken();
  const { currentProject, characters } = useStore();
  const [isGenerating, setIsGenerating] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState('');
  const [activeTab, setActiveTab] = useState<'all' | 'character' | 'organization'>('all');
  const [generateForm] = Form.useForm();
  const [generateOrgForm] = Form.useForm();
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [createType, setCreateType] = useState<'character' | 'organization'>('character');
  const [editingCharacter, setEditingCharacter] = useState<Character | null>(null);
  const [mainCareers, setMainCareers] = useState<Career[]>([]);
  const [subCareers, setSubCareers] = useState<Career[]>([]);
  const [selectedCharacters, setSelectedCharacters] = useState<string[]>([]);
  const [isImportModalOpen, setIsImportModalOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const {
    refreshCharacters,
    deleteCharacter
  } = useCharacterSync();

  useEffect(() => {
    if (currentProject?.id) {
      refreshCharacters();
      fetchCareers();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.id]);
  const [modal, contextHolder] = Modal.useModal();

  const fetchCareers = async () => {
    if (!currentProject?.id) return;
    try {
      const response = await api.get<unknown, { main_careers: Career[]; sub_careers: Career[] }>('/careers', {
        params: { project_id: currentProject.id }
      });
      setMainCareers(response.main_careers || []);
      setSubCareers(response.sub_careers || []);
    } catch (error) {
      console.error('fetch careers failed:', error);
    }
  };

  if (!currentProject) return null;

  const handleDeleteCharacter = async (id: string) => {
    try {
      await deleteCharacter(id);
      message.success(t('toast.deleteSuccess'));
    } catch {
      message.error(t('toast.deleteFailed'));
    }
  };

  const handleGenerate = async (values: { name?: string; role_type: string; background?: string }) => {
    try {
      setIsGenerating(true);
      setProgress(0);
      setProgressMessage(t('toast.progressPrepareCharacter'));

      const client = new SSEPostClient(
        '/api/characters/generate-stream',
        {
          project_id: currentProject.id,
          name: values.name,
          role_type: values.role_type,
          background: values.background,
        },
        {
          onProgress: (msg, prog) => {
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: (data) => {
            console.log('character generation complete:', data);
          },
          onError: (error) => {
            message.error(t('toast.generateFailed', { error }));
          },
          onComplete: () => {
            setProgress(100);
            setProgressMessage(t('toast.progressComplete'));
          }
        }
      );

      await client.connect();
      message.success(t('toast.aiCharacterSuccess'));
      Modal.destroyAll();
      await refreshCharacters();
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : t('toast.aiGenerateFailed');
      message.error(errorMessage);
    } finally {
      setTimeout(() => {
        setIsGenerating(false);
        setProgress(0);
        setProgressMessage('');
      }, 500);
    }
  };

  const handleGenerateOrganization = async (values: {
    name?: string;
    organization_type?: string;
    background?: string;
    requirements?: string;
  }) => {
    try {
      setIsGenerating(true);
      setProgress(0);
      setProgressMessage(t('toast.progressPrepareOrganization'));

      const client = new SSEPostClient(
        '/api/organizations/generate-stream',
        {
          project_id: currentProject.id,
          name: values.name,
          organization_type: values.organization_type,
          background: values.background,
          requirements: values.requirements,
        },
        {
          onProgress: (msg, prog) => {
            setProgress(prog);
            setProgressMessage(msg);
          },
          onResult: (data) => {
            console.log('organization generation complete:', data);
          },
          onError: (error) => {
            message.error(t('toast.generateFailed', { error }));
          },
          onComplete: () => {
            setProgress(100);
            setProgressMessage(t('toast.progressComplete'));
          }
        }
      );

      await client.connect();
      message.success(t('toast.aiOrganizationSuccess'));
      Modal.destroyAll();
      await refreshCharacters();
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : t('toast.aiGenerateFailed');
      message.error(errorMessage);
    } finally {
      setTimeout(() => {
        setIsGenerating(false);
        setProgress(0);
        setProgressMessage('');
      }, 500);
    }
  };

  const handleCreateCharacter = async (values: CharacterFormValues) => {
    try {
      const createData: CharacterCreateData = {
        project_id: currentProject.id,
        name: values.name,
        is_organization: createType === 'organization',
      };

      if (createType === 'character') {
        // 角色字段
        createData.age = values.age;
        createData.gender = values.gender;
        createData.role_type = values.role_type || 'supporting';
        createData.personality = values.personality;
        createData.appearance = values.appearance;
        createData.background = values.background;
        
        // 职业字段
        if (values.main_career_id) {
          createData.main_career_id = values.main_career_id;
          createData.main_career_stage = values.main_career_stage || 1;
        }
        
        // 处理副职业数据
        if (values.sub_career_data && Array.isArray(values.sub_career_data) && values.sub_career_data.length > 0) {
          createData.sub_careers = JSON.stringify(values.sub_career_data);
        }
      } else {
        // 组织字段
        createData.organization_type = values.organization_type;
        createData.organization_purpose = values.organization_purpose;
        createData.background = values.background;
        createData.power_level = values.power_level;
        createData.location = values.location;
        createData.motto = values.motto;
        createData.color = values.color;
        createData.role_type = 'supporting'; // 组织默认为配角
      }

      await characterApi.createCharacter(createData);
      message.success(createType === 'character' ? t('toast.characterCreated') : t('toast.organizationCreated'));
      setIsCreateModalOpen(false);
      createForm.resetFields();
      await refreshCharacters();
    } catch {
      message.error(t('toast.createFailed'));
    }
  };

  const handleEditCharacter = (character: Character) => {
    setEditingCharacter(character);

    // 提取副职业数据（包含职业ID和阶段）
    const subCareerData: SubCareerData[] = character.sub_careers?.map((sc) => ({
      career_id: sc.career_id,
      stage: sc.stage || 1
    })) || [];

    editForm.setFieldsValue({
      ...character,
      sub_career_data: subCareerData
    });
    setIsEditModalOpen(true);
  };

  const handleUpdateCharacter = async (values: CharacterFormValues) => {
    if (!editingCharacter) return;

    try {
      // 提取副职业数据，剩余的作为更新数据
      const { sub_career_data: subCareerData, ...restValues } = values;
      const updateData: CharacterUpdateData = { ...restValues };

      // 转换为sub_careers格式
      if (subCareerData && Array.isArray(subCareerData) && subCareerData.length > 0) {
        updateData.sub_careers = JSON.stringify(subCareerData);
      } else {
        updateData.sub_careers = JSON.stringify([]);
      }

      await characterApi.updateCharacter(editingCharacter.id, updateData);
      message.success(t('toast.updateSuccess'));
      setIsEditModalOpen(false);
      editForm.resetFields();
      setEditingCharacter(null);
      await refreshCharacters();
    } catch (error) {
      console.error('update failed:', error);
      message.error(t('toast.updateFailed'));
    }
  };

  const handleDeleteCharacterWrapper = (id: string) => {
    handleDeleteCharacter(id);
  };

  // 导出选中的角色/组织
  const handleExportSelected = async () => {
    if (selectedCharacters.length === 0) {
      message.warning(t('toast.selectForExport'));
      return;
    }

    try {
      await characterApi.exportCharacters(selectedCharacters);
      message.success(t('toast.exportCountSuccess', { n: selectedCharacters.length }));
      setSelectedCharacters([]);
    } catch (error) {
      message.error(t('toast.exportFailed'));
      console.error('export error:', error);
    }
  };

  // 导出单个角色/组织
  const handleExportSingle = async (characterId: string) => {
    try {
      await characterApi.exportCharacters([characterId]);
      message.success(t('toast.exportSuccess'));
    } catch (error) {
      message.error(t('toast.exportFailed'));
      console.error('export error:', error);
    }
  };

  // 处理文件选择
  const handleFileSelect = async (file: File) => {
    try {
      // 验证文件
      const validation = await characterApi.validateImportCharacters(file);
      
      if (!validation.valid) {
        modal.error({
          title: t('import.validateFailed'),
          centered: true,
          content: (
            <div>
              {validation.errors.map((error, index) => (
                <div key={index} style={{ color: token.colorError }}>• {error}</div>
              ))}
            </div>
          ),
        });
        return;
      }

      // 显示预览对话框
      modal.confirm({
        title: t('import.previewTitle'),
        width: 500,
        centered: true,
        content: (
          <div>
            <p><strong>{t('import.fileVersion')}</strong> {validation.version}</p>
            <Divider style={{ margin: '12px 0' }} />
            <p><strong>{t('import.willImport')}</strong></p>
            <ul style={{ marginLeft: 20 }}>
              <li>{t('import.charactersCount', { n: validation.statistics.characters })}</li>
              <li>{t('import.organizationsCount', { n: validation.statistics.organizations })}</li>
            </ul>
            {validation.warnings.length > 0 && (
              <>
                <Divider style={{ margin: '12px 0' }} />
                <p style={{ color: token.colorWarning }}><strong>{t('import.warningsLabel')}</strong></p>
                <ul style={{ marginLeft: 20 }}>
                  {validation.warnings.map((warning, index) => (
                    <li key={index} style={{ color: token.colorWarning }}>{warning}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        ),
        okText: t('import.confirmImport'),
        cancelText: t('buttons.cancel'),
        onOk: async () => {
          try {
            const result = await characterApi.importCharacters(currentProject.id, file);
            
            if (result.success) {
              // 显示导入结果
              modal.success({
                title: t('import.doneTitle'),
                width: 600,
                centered: true,
                content: (
                  <div>
                    <p><strong>{t('import.successCount', { n: result.statistics.imported })}</strong></p>
                    {result.details.imported_characters.length > 0 && (
                      <>
                        <p style={{ marginTop: 12, marginBottom: 4 }}>{t('import.charactersLabel')}</p>
                        <ul style={{ marginLeft: 20 }}>
                          {result.details.imported_characters.map((name, index) => (
                            <li key={index}>{name}</li>
                          ))}
                        </ul>
                      </>
                    )}
                    {result.details.imported_organizations.length > 0 && (
                      <>
                        <p style={{ marginTop: 12, marginBottom: 4 }}>{t('import.organizationsLabel')}</p>
                        <ul style={{ marginLeft: 20 }}>
                          {result.details.imported_organizations.map((name, index) => (
                            <li key={index}>{name}</li>
                          ))}
                        </ul>
                      </>
                    )}
                    {result.statistics.skipped > 0 && (
                      <>
                        <Divider style={{ margin: '12px 0' }} />
                        <p style={{ color: token.colorWarning }}>{t('import.skippedCount', { n: result.statistics.skipped })}</p>
                        <ul style={{ marginLeft: 20 }}>
                          {result.details.skipped.map((name, index) => (
                            <li key={index} style={{ color: token.colorWarning }}>{name}</li>
                          ))}
                        </ul>
                      </>
                    )}
                    {result.warnings.length > 0 && (
                      <>
                        <Divider style={{ margin: '12px 0' }} />
                        <p style={{ color: token.colorWarning }}>{t('import.warningsLabel')}</p>
                        <ul style={{ marginLeft: 20 }}>
                          {result.warnings.map((warning, index) => (
                            <li key={index} style={{ color: token.colorWarning }}>{warning}</li>
                          ))}
                        </ul>
                      </>
                    )}
                    {result.details.errors.length > 0 && (
                      <>
                        <Divider style={{ margin: '12px 0' }} />
                        <p style={{ color: token.colorError }}>{t('import.failedCount', { n: result.statistics.errors })}</p>
                        <ul style={{ marginLeft: 20 }}>
                          {result.details.errors.map((error, index) => (
                            <li key={index} style={{ color: token.colorError }}>{error}</li>
                          ))}
                        </ul>
                      </>
                    )}
                  </div>
                ),
              });
              
              // 刷新列表
              await refreshCharacters();
              setIsImportModalOpen(false);
            } else {
              message.error(result.message || t('toast.importFailed'));
            }
          } catch (error: unknown) {
            const apiError = error as ApiError;
            message.error(apiError.response?.data?.detail || t('toast.importFailed'));
            console.error('import error:', error);
          }
        },
      });
    } catch (error: unknown) {
      const apiError = error as ApiError;
      message.error(apiError.response?.data?.detail || t('import.validateFailed'));
      console.error('validation error:', error);
    }
  };

  // 切换选择
  const toggleSelectCharacter = (id: string) => {
    setSelectedCharacters(prev =>
      prev.includes(id) ? prev.filter(cid => cid !== id) : [...prev, id]
    );
  };

  // 全选/取消全选
  const toggleSelectAll = () => {
    if (selectedCharacters.length === displayList.length) {
      setSelectedCharacters([]);
    } else {
      setSelectedCharacters(displayList.map(c => c.id));
    }
  };

  const showGenerateModal = () => {
    modal.confirm({
      title: t('generateModal.characterTitle'),
      width: 600,
      centered: true,
      content: (
        <Form form={generateForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            label={t('form.nameCharacter')}
            name="name"
          >
            <Input placeholder={t('placeholder.nameCharacterOptional')} />
          </Form.Item>
          <Form.Item
            label={t('form.roleType')}
            name="role_type"
            rules={[{ required: true, message: t('validation.roleTypeRequired') }]}
          >
            <Select placeholder={t('placeholder.selectRoleType')}>
              <Select.Option value="protagonist">{t('role.protagonist')}</Select.Option>
              <Select.Option value="supporting">{t('role.supporting')}</Select.Option>
              <Select.Option value="antagonist">{t('role.antagonist')}</Select.Option>
            </Select>
          </Form.Item>
          <Form.Item label={t('form.backgroundSetting')} name="background">
            <TextArea rows={3} placeholder={t('placeholder.backgroundForGen')} />
          </Form.Item>
        </Form>
      ),
      okText: t('buttons.generate'),
      cancelText: t('buttons.cancel'),
      onOk: async () => {
        const values = await generateForm.validateFields();
        await handleGenerate(values);
      },
    });
  };

  const showGenerateOrgModal = () => {
    modal.confirm({
      title: t('generateModal.organizationTitle'),
      width: 600,
      centered: true,
      content: (
        <Form form={generateOrgForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            label={t('form.nameOrganization')}
            name="name"
          >
            <Input placeholder={t('placeholder.nameOrganizationOptional')} />
          </Form.Item>
          <Form.Item
            label={t('form.organizationType')}
            name="organization_type"
          >
            <Input placeholder={t('placeholder.organizationTypeForGen')} />
          </Form.Item>
          <Form.Item label={t('form.backgroundSetting')} name="background">
            <TextArea rows={3} placeholder={t('placeholder.organizationBackgroundForGen')} />
          </Form.Item>
          <Form.Item label={t('form.requirements')} name="requirements">
            <TextArea rows={2} placeholder={t('placeholder.requirements')} />
          </Form.Item>
        </Form>
      ),
      okText: t('buttons.generate'),
      cancelText: t('buttons.cancel'),
      onOk: async () => {
        const values = await generateOrgForm.validateFields();
        await handleGenerateOrganization(values);
      },
    });
  };

  const characterList = characters.filter(c => !c.is_organization);
  const organizationList = characters.filter(c => c.is_organization);

  const getDisplayList = () => {
    if (activeTab === 'character') return characterList;
    if (activeTab === 'organization') return organizationList;
    return characters;
  };

  const displayList = getDisplayList();

  const isMobile = window.innerWidth <= 768;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {contextHolder}
      <div style={{
        position: 'sticky',
        top: 0,
        zIndex: 10,
        backgroundColor: 'var(--color-bg-container)',
        padding: isMobile ? '12px 0' : '16px 0',
        marginBottom: isMobile ? 12 : 16,
        borderBottom: '1px solid var(--color-border-secondary)',
        display: 'flex',
        flexDirection: isMobile ? 'column' : 'row',
        gap: isMobile ? 12 : 0,
        justifyContent: 'space-between',
        alignItems: isMobile ? 'stretch' : 'center'
      }}>
        <h2 style={{ margin: 0, fontSize: isMobile ? 18 : 24 }}>
          <TeamOutlined style={{ marginRight: 8 }} />
          {t('title')}
        </h2>
        <Space wrap>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setCreateType('character');
              setIsCreateModalOpen(true);
            }}
            size={isMobile ? 'small' : 'middle'}
          >
            {t('buttons.createCharacter')}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setCreateType('organization');
              setIsCreateModalOpen(true);
            }}
            size={isMobile ? 'small' : 'middle'}
          >
            {t('buttons.createOrganization')}
          </Button>
          <Button
            type="dashed"
            icon={<ThunderboltOutlined />}
            onClick={showGenerateModal}
            loading={isGenerating}
            size={isMobile ? 'small' : 'middle'}
          >
            {t('buttons.aiGenerateCharacter')}
          </Button>
          <Button
            type="dashed"
            icon={<ThunderboltOutlined />}
            onClick={showGenerateOrgModal}
            loading={isGenerating}
            size={isMobile ? 'small' : 'middle'}
          >
            {t('buttons.aiGenerateOrganization')}
          </Button>
          <Button
            icon={<ImportOutlined />}
            onClick={() => setIsImportModalOpen(true)}
            size={isMobile ? 'small' : 'middle'}
          >
            {t('buttons.import')}
          </Button>
          {selectedCharacters.length > 0 && (
            <Button
              icon={<ExportOutlined />}
              onClick={handleExportSelected}
              size={isMobile ? 'small' : 'middle'}
            >
              {t('buttons.exportBatch', { n: selectedCharacters.length })}
            </Button>
          )}
        </Space>
      </div>

      {characters.length > 0 && (
        <div style={{
          position: 'sticky',
          top: isMobile ? 60 : 72,
          zIndex: 9,
          backgroundColor: 'var(--color-bg-container)',
          paddingBottom: 8,
          borderBottom: '1px solid var(--color-border-secondary)',
        }}>
          <Tabs
            activeKey={activeTab}
            onChange={(key) => setActiveTab(key as 'all' | 'character' | 'organization')}
            items={[
              {
                key: 'all',
                label: t('tabs.all', { n: characters.length }),
              },
              {
                key: 'character',
                label: (
                  <span>
                    <UserOutlined /> {t('tabs.characters', { n: characterList.length })}
                  </span>
                ),
              },
              {
                key: 'organization',
                label: (
                  <span>
                    <TeamOutlined /> {t('tabs.organizations', { n: organizationList.length })}
                  </span>
                ),
              },
            ]}
          />
        </div>
      )}

      {/* 批量选择工具栏 */}
      {characters.length > 0 && (
        <div style={{
          position: 'sticky',
          top: isMobile ? 120 : 132,
          zIndex: 8,
          backgroundColor: 'var(--color-bg-container)',
          paddingBottom: 8,
          paddingTop: 8,
          marginTop: 8,
          borderBottom: selectedCharacters.length > 0 ? '1px solid var(--color-border-secondary)' : 'none',
        }}>
          <Space>
            <Checkbox
              checked={selectedCharacters.length === displayList.length && displayList.length > 0}
              indeterminate={selectedCharacters.length > 0 && selectedCharacters.length < displayList.length}
              onChange={toggleSelectAll}
            >
              {selectedCharacters.length > 0 ? t('toolbar.selected', { n: selectedCharacters.length }) : t('toolbar.selectAll')}
            </Checkbox>
            {selectedCharacters.length > 0 && (
              <Button
                type="link"
                size="small"
                onClick={() => setSelectedCharacters([])}
              >
                {t('toolbar.clearSelection')}
              </Button>
            )}
          </Space>
        </div>
      )}

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {characters.length === 0 ? (
          <Empty description={t('empty.none')} />
        ) : (
          <>
            <Row gutter={isMobile ? [8, 8] : charactersPageGridConfig.gutter}>
              {activeTab === 'all' && (
                <>
                  {characterList.length > 0 && (
                    <>
                      <Col span={24}>
                        <Divider orientation="left">
                          <Title level={5} style={{ margin: 0 }}>
                            <UserOutlined style={{ marginRight: 8 }} />
                            {t('tabs.characters', { n: characterList.length })}
                          </Title>
                        </Divider>
                      </Col>
                      {characterList.map((character) => (
                        <Col
                          xs={24}
                          sm={charactersPageGridConfig.sm}
                          md={charactersPageGridConfig.md}
                          lg={charactersPageGridConfig.lg}
                          xl={charactersPageGridConfig.xl}
                          key={character.id}
                          style={{ padding: isMobile ? '4px' : '8px' }}
                        >
                          <div style={{ position: 'relative' }}>
                            <Checkbox
                              checked={selectedCharacters.includes(character.id)}
                              onChange={() => toggleSelectCharacter(character.id)}
                              style={{ position: 'absolute', top: 8, left: 8, zIndex: 1 }}
                            />
                            <CharacterCard
                              character={character}
                              onEdit={handleEditCharacter}
                              onDelete={handleDeleteCharacterWrapper}
                              onExport={() => handleExportSingle(character.id)}
                            />
                          </div>
                        </Col>
                      ))}
                    </>
                  )}

                  {organizationList.length > 0 && (
                    <>
                      <Col span={24}>
                        <Divider orientation="left">
                          <Title level={5} style={{ margin: 0 }}>
                            <TeamOutlined style={{ marginRight: 8 }} />
                            {t('tabs.organizations', { n: organizationList.length })}
                          </Title>
                        </Divider>
                      </Col>
                      {organizationList.map((org) => (
                        <Col
                          xs={24}
                          sm={charactersPageGridConfig.sm}
                          md={charactersPageGridConfig.md}
                          lg={charactersPageGridConfig.lg}
                          xl={charactersPageGridConfig.xl}
                          key={org.id}
                          style={{ padding: isMobile ? '4px' : '8px' }}
                        >
                          <div style={{ position: 'relative' }}>
                            <Checkbox
                              checked={selectedCharacters.includes(org.id)}
                              onChange={() => toggleSelectCharacter(org.id)}
                              style={{ position: 'absolute', top: 8, left: 8, zIndex: 1 }}
                            />
                            <CharacterCard
                              character={org}
                              onEdit={handleEditCharacter}
                              onDelete={handleDeleteCharacterWrapper}
                              onExport={() => handleExportSingle(org.id)}
                            />
                          </div>
                        </Col>
                      ))}
                    </>
                  )}
                </>
              )}

              {activeTab === 'character' && characterList.map((character) => (
                <Col
                  xs={24}
                  sm={charactersPageGridConfig.sm}
                  md={charactersPageGridConfig.md}
                  lg={charactersPageGridConfig.lg}
                  xl={charactersPageGridConfig.xl}
                  key={character.id}
                  style={{ padding: isMobile ? '4px' : '8px' }}
                >
                  <div style={{ position: 'relative' }}>
                    <Checkbox
                      checked={selectedCharacters.includes(character.id)}
                      onChange={() => toggleSelectCharacter(character.id)}
                      style={{ position: 'absolute', top: 8, left: 8, zIndex: 1 }}
                    />
                    <CharacterCard
                      character={character}
                      onEdit={handleEditCharacter}
                      onDelete={handleDeleteCharacterWrapper}
                      onExport={() => handleExportSingle(character.id)}
                    />
                  </div>
                </Col>
              ))}

              {activeTab === 'organization' && organizationList.map((org) => (
                <Col
                  xs={24}
                  sm={charactersPageGridConfig.sm}
                  md={charactersPageGridConfig.md}
                  lg={charactersPageGridConfig.lg}
                  xl={charactersPageGridConfig.xl}
                  key={org.id}
                  style={{ padding: isMobile ? '4px' : '8px' }}
                >
                  <div style={{ position: 'relative' }}>
                    <Checkbox
                      checked={selectedCharacters.includes(org.id)}
                      onChange={() => toggleSelectCharacter(org.id)}
                      style={{ position: 'absolute', top: 8, left: 8, zIndex: 1 }}
                    />
                    <CharacterCard
                      character={org}
                      onEdit={handleEditCharacter}
                      onDelete={handleDeleteCharacterWrapper}
                      onExport={() => handleExportSingle(org.id)}
                    />
                  </div>
                </Col>
              ))}
            </Row>

            {displayList.length === 0 && (
              <Empty
                description={
                  activeTab === 'character'
                    ? t('empty.noCharacters')
                    : activeTab === 'organization'
                      ? t('empty.noOrganizations')
                      : t('empty.noData')
                }
              />
            )}
          </>
        )}
      </div>

      <Modal
        title={editingCharacter?.is_organization ? t('editModal.organizationTitle') : t('editModal.characterTitle')}
        open={isEditModalOpen}
        onCancel={() => {
          setIsEditModalOpen(false);
          editForm.resetFields();
          setEditingCharacter(null);
        }}
        footer={
          <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
            <Button onClick={() => {
              setIsEditModalOpen(false);
              editForm.resetFields();
              setEditingCharacter(null);
            }}>
              {t('buttons.cancel')}
            </Button>
            <Button type="primary" onClick={() => editForm.submit()}>
              {t('buttons.save')}
            </Button>
          </Space>
        }
        centered
        width={isMobile ? '100%' : 700}
        style={isMobile ? { top: 0, paddingBottom: 0, maxWidth: '100vw' } : undefined}
        styles={{
          body: {
            maxHeight: isMobile ? 'calc(100vh - 110px)' : 'calc(100vh - 200px)',
            overflowY: 'auto',
            overflowX: 'hidden'
          }
        }}
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdateCharacter} style={{ marginTop: 8 }}>
          {!editingCharacter?.is_organization ? (
            <>
              {/* 编辑角色 - 第一行：名称、定位、年龄、性别 */}
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item
                    label={t('form.nameCharacter')}
                    name="name"
                    rules={[{ required: true, message: t('validation.nameCharacterRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('form.nameCharacter')} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item label={t('form.roleType')} name="role_type" style={{ marginBottom: 12 }}>
                    <Select>
                      <Select.Option value="protagonist">{t('role.protagonist')}</Select.Option>
                      <Select.Option value="supporting">{t('role.supporting')}</Select.Option>
                      <Select.Option value="antagonist">{t('role.antagonist')}</Select.Option>
                    </Select>
                  </Form.Item>
                </Col>
                <Col span={5}>
                  <Form.Item label={t('form.age')} name="age" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.age')} />
                  </Form.Item>
                </Col>
                <Col span={5}>
                  <Form.Item label={t('form.gender')} name="gender" style={{ marginBottom: 12 }}>
                    <Select placeholder={t('form.gender')}>
                      <Select.Option value="男">{t('gender.male')}</Select.Option>
                      <Select.Option value="女">{t('gender.female')}</Select.Option>
                      <Select.Option value="其他">{t('gender.other')}</Select.Option>
                    </Select>
                  </Form.Item>
                </Col>
              </Row>

              {/* 第二行：性格特点、外貌描写 */}
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item label={t('form.personality')} name="personality" style={{ marginBottom: 12 }}>
                    <TextArea rows={2} placeholder={t('placeholder.personality')} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label={t('form.appearance')} name="appearance" style={{ marginBottom: 12 }}>
                    <TextArea rows={2} placeholder={t('placeholder.appearance')} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 人际关系（只读，由关系管理页面维护） */}
              {editingCharacter?.relationships && (
                <Form.Item label={t('form.relationshipsReadonly')} style={{ marginBottom: 12 }}>
                  <Input.TextArea
                    value={editingCharacter.relationships}
                    readOnly
                    autoSize={{ minRows: 1, maxRows: 3 }}
                    style={{ backgroundColor: token.colorFillTertiary, cursor: 'default' }}
                  />
                </Form.Item>
              )}

              {/* 第四行：角色背景 */}
              <Form.Item label={t('form.backgroundCharacter')} name="background" style={{ marginBottom: 12 }}>
                <TextArea rows={2} placeholder={t('placeholder.backgroundCharacter')} />
              </Form.Item>

              {/* 职业信息 */}
              {(mainCareers.length > 0 || subCareers.length > 0) && (
                <>
                  <Divider style={{ margin: '8px 0' }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t('form.careerInfo')}</Typography.Text>
                  </Divider>
                  {mainCareers.length > 0 && (
                    <Row gutter={12}>
                      <Col span={16}>
                        <Form.Item label={t('form.mainCareer')} name="main_career_id" tooltip={t('form.mainCareerTooltip')} style={{ marginBottom: 12 }}>
                          <Select placeholder={t('placeholder.selectMainCareer')} allowClear size="small">
                            {mainCareers.map(career => (
                              <Select.Option key={career.id} value={career.id}>
                                {career.name}{t('form.careerMaxStage', { stage: career.max_stage })}
                              </Select.Option>
                            ))}
                          </Select>
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item label={t('form.currentStage')} name="main_career_stage" tooltip={t('form.currentStageTooltip')} style={{ marginBottom: 12 }}>
                          <InputNumber
                            min={1}
                            max={editForm.getFieldValue('main_career_id') ?
                              mainCareers.find(c => c.id === editForm.getFieldValue('main_career_id'))?.max_stage || 10
                              : 10}
                            style={{ width: '100%' }}
                            placeholder={t('placeholder.stage')}
                            size="small"
                          />
                        </Form.Item>
                      </Col>
                    </Row>
                  )}
                  {subCareers.length > 0 && (
                    <Form.List name="sub_career_data">
                      {(fields, { add, remove }) => (
                        <>
                          <div style={{ marginBottom: 4 }}>
                            <Typography.Text strong style={{ fontSize: 12 }}>{t('form.subCareers')}</Typography.Text>
                          </div>
                          <div style={{ maxHeight: '80px', overflowY: 'auto', overflowX: 'hidden', marginBottom: 8, paddingRight: 8 }}>
                            {fields.map((field) => (
                              <Row key={field.key} gutter={8} style={{ marginBottom: 4 }}>
                                <Col span={16}>
                                  <Form.Item
                                    {...field}
                                    name={[field.name, 'career_id']}
                                    rules={[{ required: true, message: t('validation.subCareerRequired') }]}
                                    style={{ marginBottom: 0 }}
                                  >
                                    <Select placeholder={t('placeholder.selectSubCareer')} size="small">
                                      {subCareers.map(career => (
                                        <Select.Option key={career.id} value={career.id}>
                                          {career.name}{t('form.careerMaxStage', { stage: career.max_stage })}
                                        </Select.Option>
                                      ))}
                                    </Select>
                                  </Form.Item>
                                </Col>
                                <Col span={5}>
                                  <Form.Item
                                    {...field}
                                    name={[field.name, 'stage']}
                                    rules={[{ required: true, message: t('validation.stageRequired') }]}
                                    style={{ marginBottom: 0 }}
                                  >
                                    <InputNumber
                                      min={1}
                                      max={(() => {
                                        const careerId = editForm.getFieldValue(['sub_career_data', field.name, 'career_id']);
                                        const career = subCareers.find(c => c.id === careerId);
                                        return career?.max_stage || 10;
                                      })()}
                                      placeholder={t('placeholder.stage')}
                                      style={{ width: '100%' }}
                                      size="small"
                                    />
                                  </Form.Item>
                                </Col>
                                <Col span={3}>
                                  <Button
                                    type="text"
                                    danger
                                    size="small"
                                    onClick={() => remove(field.name)}
                                  >
                                    {t('buttons.delete')}
                                  </Button>
                                </Col>
                              </Row>
                            ))}
                          </div>
                          <Button
                            type="dashed"
                            onClick={() => add({ career_id: undefined, stage: 1 })}
                            block
                            size="small"
                          >
                            {t('buttons.addSubCareer')}
                          </Button>
                        </>
                      )}
                    </Form.List>
                  )}
                </>
              )}
            </>
          ) : (
            <>
              {/* 编辑组织 - 第一行：名称、类型、势力等级 */}
              <Row gutter={12}>
                <Col span={10}>
                  <Form.Item
                    label={t('form.nameOrganization')}
                    name="name"
                    rules={[{ required: true, message: t('validation.nameOrganizationRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('form.nameOrganization')} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item
                    label={t('form.organizationType')}
                    name="organization_type"
                    rules={[{ required: true, message: t('validation.organizationTypeRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('placeholder.organizationType')} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item
                    label={t('form.powerLevel')}
                    name="power_level"
                    tooltip={t('form.powerLevelTooltip')}
                    style={{ marginBottom: 12 }}
                  >
                    <InputNumber min={0} max={100} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 第二行：组织目的 */}
              <Form.Item
                label={t('form.organizationPurpose')}
                name="organization_purpose"
                rules={[{ required: true, message: t('validation.organizationPurposeRequired') }]}
                style={{ marginBottom: 12 }}
              >
                <Input placeholder={t('placeholder.organizationPurpose')} />
              </Form.Item>

              {/* 第三行：主要成员（只读展示） */}
              <Form.Item
                label={t('form.organizationMembers')}
                name="organization_members"
                style={{ marginBottom: 4 }}
                tooltip={t('form.membersTooltip')}
              >
                <TextArea
                  disabled
                  autoSize={{ minRows: 1, maxRows: 4 }}
                  placeholder={t('form.membersEmpty')}
                  style={{ color: token.colorText, backgroundColor: token.colorFillAlter }}
                />
              </Form.Item>
              <div style={{ marginBottom: 12, fontSize: 12, color: token.colorTextTertiary }}>
                {t('form.membersHint')}
              </div>

              {/* 第四行：所在地、代表颜色 */}
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item label={t('form.location')} name="location" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.location')} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label={t('form.color')} name="color" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.color')} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 第四行：格言/口号 */}
              <Form.Item label={t('form.motto')} name="motto" style={{ marginBottom: 12 }}>
                <Input placeholder={t('placeholder.motto')} />
              </Form.Item>

              {/* 第五行：组织背景 */}
              <Form.Item label={t('form.backgroundOrganization')} name="background" style={{ marginBottom: 12 }}>
                <TextArea rows={2} placeholder={t('placeholder.backgroundOrganization')} />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>

      {/* 手动创建角色/组织模态框 */}
      <Modal
        title={createType === 'character' ? t('buttons.createCharacter') : t('buttons.createOrganization')}
        open={isCreateModalOpen}
        onCancel={() => {
          setIsCreateModalOpen(false);
          createForm.resetFields();
        }}
        footer={null}
        centered
        width={isMobile ? '100%' : 700}
        style={isMobile ? { top: 0, paddingBottom: 0, maxWidth: '100vw' } : undefined}
        styles={{
          body: {
            maxHeight: isMobile ? 'calc(100vh - 110px)' : 'calc(100vh - 200px)',
            overflowY: 'auto',
            overflowX: 'hidden'
          }
        }}
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreateCharacter} style={{ marginTop: 8 }}>
          {createType === 'character' ? (
            <>
              {/* 角色基本信息 - 第一行：名称、定位、年龄、性别 */}
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item
                    label={t('form.nameCharacter')}
                    name="name"
                    rules={[{ required: true, message: t('validation.nameCharacterRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('form.nameCharacter')} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item label={t('form.roleType')} name="role_type" initialValue="supporting" style={{ marginBottom: 12 }}>
                    <Select>
                      <Select.Option value="protagonist">{t('role.protagonist')}</Select.Option>
                      <Select.Option value="supporting">{t('role.supporting')}</Select.Option>
                      <Select.Option value="antagonist">{t('role.antagonist')}</Select.Option>
                    </Select>
                  </Form.Item>
                </Col>
                <Col span={5}>
                  <Form.Item label={t('form.age')} name="age" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.age')} />
                  </Form.Item>
                </Col>
                <Col span={5}>
                  <Form.Item label={t('form.gender')} name="gender" style={{ marginBottom: 12 }}>
                    <Select placeholder={t('form.gender')}>
                      <Select.Option value="男">{t('gender.male')}</Select.Option>
                      <Select.Option value="女">{t('gender.female')}</Select.Option>
                      <Select.Option value="其他">{t('gender.other')}</Select.Option>
                    </Select>
                  </Form.Item>
                </Col>
              </Row>

              {/* 第二行：性格特点、外貌描写 */}
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item label={t('form.personality')} name="personality" style={{ marginBottom: 12 }}>
                    <TextArea rows={2} placeholder={t('placeholder.personality')} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label={t('form.appearance')} name="appearance" style={{ marginBottom: 12 }}>
                    <TextArea rows={2} placeholder={t('placeholder.appearance')} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 第三行：角色背景 */}
              <Form.Item label={t('form.backgroundCharacter')} name="background" style={{ marginBottom: 12 }}>
                <TextArea rows={2} placeholder={t('placeholder.backgroundCharacter')} />
              </Form.Item>

              {/* 职业信息 - 折叠区域 */}
              {(mainCareers.length > 0 || subCareers.length > 0) && (
                <>
                  <Divider style={{ margin: '8px 0' }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t('form.careerInfoOptional')}</Typography.Text>
                  </Divider>
                  {mainCareers.length > 0 && (
                    <Row gutter={12}>
                      <Col span={16}>
                        <Form.Item label={t('form.mainCareer')} name="main_career_id" tooltip={t('form.mainCareerTooltip')} style={{ marginBottom: 12 }}>
                          <Select placeholder={t('placeholder.selectMainCareer')} allowClear size="small">
                            {mainCareers.map(career => (
                              <Select.Option key={career.id} value={career.id}>
                                {career.name}{t('form.careerMaxStage', { stage: career.max_stage })}
                              </Select.Option>
                            ))}
                          </Select>
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item label={t('form.currentStage')} name="main_career_stage" tooltip={t('form.currentStageTooltip')} style={{ marginBottom: 12 }}>
                          <InputNumber
                            min={1}
                            max={createForm.getFieldValue('main_career_id') ?
                              mainCareers.find(c => c.id === createForm.getFieldValue('main_career_id'))?.max_stage || 10
                              : 10}
                            style={{ width: '100%' }}
                            placeholder={t('placeholder.stage')}
                            size="small"
                          />
                        </Form.Item>
                      </Col>
                    </Row>
                  )}
                  {subCareers.length > 0 && (
                    <Form.List name="sub_career_data">
                      {(fields, { add, remove }) => (
                        <>
                          <div style={{ marginBottom: 4 }}>
                            <Typography.Text strong style={{ fontSize: 12 }}>{t('form.subCareers')}</Typography.Text>
                          </div>
                          <div style={{ maxHeight: '80px', overflowY: 'auto', overflowX: 'hidden', marginBottom: 8, paddingRight: 8 }}>
                            {fields.map((field) => (
                              <Row key={field.key} gutter={8} style={{ marginBottom: 4 }}>
                                <Col span={16}>
                                  <Form.Item
                                    {...field}
                                    name={[field.name, 'career_id']}
                                    rules={[{ required: true, message: t('validation.subCareerRequired') }]}
                                    style={{ marginBottom: 0 }}
                                  >
                                    <Select placeholder={t('placeholder.selectSubCareer')} size="small">
                                      {subCareers.map(career => (
                                        <Select.Option key={career.id} value={career.id}>
                                          {career.name}{t('form.careerMaxStage', { stage: career.max_stage })}
                                        </Select.Option>
                                      ))}
                                    </Select>
                                  </Form.Item>
                                </Col>
                                <Col span={5}>
                                  <Form.Item
                                    {...field}
                                    name={[field.name, 'stage']}
                                    rules={[{ required: true, message: t('validation.stageRequired') }]}
                                    style={{ marginBottom: 0 }}
                                  >
                                    <InputNumber
                                      min={1}
                                      max={(() => {
                                        const careerId = createForm.getFieldValue(['sub_career_data', field.name, 'career_id']);
                                        const career = subCareers.find(c => c.id === careerId);
                                        return career?.max_stage || 10;
                                      })()}
                                      placeholder={t('placeholder.stage')}
                                      style={{ width: '100%' }}
                                      size="small"
                                    />
                                  </Form.Item>
                                </Col>
                                <Col span={3}>
                                  <Button
                                    type="text"
                                    danger
                                    size="small"
                                    onClick={() => remove(field.name)}
                                  >
                                    {t('buttons.delete')}
                                  </Button>
                                </Col>
                              </Row>
                            ))}
                          </div>
                          <Button
                            type="dashed"
                            onClick={() => add({ career_id: undefined, stage: 1 })}
                            block
                            size="small"
                          >
                            {t('buttons.addSubCareer')}
                          </Button>
                        </>
                      )}
                    </Form.List>
                  )}
                </>
              )}
            </>
          ) : (
            <>
              {/* 组织基本信息 - 第一行：名称、类型、势力等级 */}
              <Row gutter={12}>
                <Col span={10}>
                  <Form.Item
                    label={t('form.nameOrganization')}
                    name="name"
                    rules={[{ required: true, message: t('validation.nameOrganizationRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('form.nameOrganization')} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item
                    label={t('form.organizationType')}
                    name="organization_type"
                    rules={[{ required: true, message: t('validation.organizationTypeRequired') }]}
                    style={{ marginBottom: 12 }}
                  >
                    <Input placeholder={t('placeholder.organizationType')} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item
                    label={t('form.powerLevel')}
                    name="power_level"
                    initialValue={50}
                    tooltip={t('form.powerLevelTooltip')}
                    style={{ marginBottom: 12 }}
                  >
                    <InputNumber min={0} max={100} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 第二行：组织目的 */}
              <Form.Item
                label={t('form.organizationPurpose')}
                name="organization_purpose"
                rules={[{ required: true, message: t('validation.organizationPurposeRequired') }]}
                style={{ marginBottom: 12 }}
              >
                <Input placeholder={t('placeholder.organizationPurpose')} />
              </Form.Item>

              {/* 第三行：所在地、代表颜色 */}
              <Row gutter={12}>
                <Col span={12}>
                  <Form.Item label={t('form.location')} name="location" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.location')} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item label={t('form.color')} name="color" style={{ marginBottom: 12 }}>
                    <Input placeholder={t('placeholder.color')} />
                  </Form.Item>
                </Col>
              </Row>

              {/* 第四行：格言/口号 */}
              <Form.Item label={t('form.motto')} name="motto" style={{ marginBottom: 12 }}>
                <Input placeholder={t('placeholder.motto')} />
              </Form.Item>

              {/* 第五行：组织背景 */}
              <Form.Item label={t('form.backgroundOrganization')} name="background" style={{ marginBottom: 12 }}>
                <TextArea rows={2} placeholder={t('placeholder.backgroundOrganization')} />
              </Form.Item>
            </>
          )}

          <Form.Item style={{ marginBottom: 0, marginTop: 16 }}>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsCreateModalOpen(false);
                createForm.resetFields();
              }}>
                {t('buttons.cancel')}
              </Button>
              <Button type="primary" htmlType="submit">
                {t('buttons.create')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 导入对话框 */}
      <Modal
        title={t('importModal.title')}
        open={isImportModalOpen}
        onCancel={() => setIsImportModalOpen(false)}
        footer={null}
        width={500}
        centered
      >
        <div style={{ textAlign: 'center', padding: '40px 20px' }}>
          <DownloadOutlined style={{ fontSize: 48, color: '#1890ff', marginBottom: 16 }} />
          <p style={{ fontSize: 16, marginBottom: 24 }}>
            {t('importModal.description')}
          </p>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            style={{ display: 'none' }}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) {
                handleFileSelect(file);
                e.target.value = ''; // 清空input，允许重复选择同一文件
              }
            }}
          />
          <Button
            type="primary"
            size="large"
            icon={<ImportOutlined />}
            onClick={() => fileInputRef.current?.click()}
          >
            {t('importModal.selectFile')}
          </Button>
          <Divider />
          <div style={{ textAlign: 'left', fontSize: 12, color: '#666' }}>
            <p style={{ marginBottom: 8 }}><strong>{t('importModal.notesTitle')}</strong></p>
            <ul style={{ marginLeft: 20 }}>
              <li>{t('importModal.noteJsonFormat')}</li>
              <li>{t('importModal.noteSkipDuplicates')}</li>
              <li>{t('importModal.noteIgnoreCareers')}</li>
            </ul>
          </div>
        </div>
      </Modal>

      {/* SSE进度显示 */}
      <SSELoadingOverlay
        loading={isGenerating}
        progress={progress}
        message={progressMessage}
      />
    </div>
  );
}