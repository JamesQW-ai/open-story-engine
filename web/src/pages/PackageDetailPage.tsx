import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  prologueText,
  identityText,
  identityEntry,
  identityCharacters,
} from '../components/storyPresentation'
import { api } from '../api/client'
import type { PackageCatalog } from '../api/types'
import { Loading, Notice, storyImage } from '../components/StoryUI'

const identityGroups = [
  { id: 'protagonist_group', label: '主角团' },
  { id: 'key_supporting', label: '主要配角' },
  { id: 'antagonist_group', label: '反派与敌对势力' },
]

export function PackageDetailPage() {
  const { packageId = '', version = '' } = useParams()
  const navigate = useNavigate()
  const [catalog, setCatalog] = useState<PackageCatalog | null>(null)
  const [error, setError] = useState('')
  const [step, setStep] = useState(0)
  const [character, setCharacter] = useState('')
  const [selectedGroup, setSelectedGroup] = useState('')
  const [mode, setMode] = useState<'source' | 'new'>('source')
  const [profile, setProfile] = useState<Record<string, string | number>>({})
  const [starting, setStarting] = useState(false)
  const [playable, setPlayable] = useState(false)
  const attempt = useRef<{ signature: string; id: string } | null>(null)
  const load = () => {
    setError('')
    api
      .getPackage(packageId, version)
      .then((c) => {
        setCatalog(c)
        const people = identityCharacters(c)
        setCharacter(people[0]?.id ?? '')
        setSelectedGroup(people[0]?.roleGroup ?? 'key_supporting')
      })
      .catch(() => setError('故事暂时无法打开，请稍后重试。'))
    api
      .health()
      .then((h) => setPlayable(h.phase === 'play' && h.state_updates_available))
      .catch(() => setError('故事暂时无法打开，请稍后重试。'))
  }
  useEffect(load, [packageId, version])
  if (!catalog)
    return (
      <main className="page">
        {error ? <Notice text={error} retry={load} /> : <Loading />}
      </main>
    )
  const entry =
    mode === 'source'
      ? identityEntry(catalog, character)
      : (catalog.entries.find((e) => e.available_to_new_character) ??
        catalog.entries[0])
  const openingAvailable =
    !!entry && (mode === 'source' || entry.available_to_new_character)
  const people = identityCharacters(catalog)
  const person = people.find((c) => c.id === character)
  const groups = identityGroups
    .map((group) => ({
      ...group,
      members: people.filter(
        (c) => (c.roleGroup ?? 'key_supporting') === group.id,
      ),
    }))
    .filter((group) => group.members.length > 0)
  const activeGroup =
    groups.find((group) => group.id === selectedGroup) ?? groups[0]
  const complete =
    mode === 'source'
      ? !!person
      : !!catalog.new_character?.enabled &&
        catalog.new_character.profile_fields.every((f) => {
          const v = profile[f.id]
          return f.type === 'integer'
            ? typeof v === 'number' &&
                Number.isInteger(v) &&
                v >= (f.minimum ?? -Infinity) &&
                v <= (f.maximum ?? Infinity)
            : typeof v === 'string' &&
                v.trim().length >= (f.minLength ?? 1) &&
                v.trim().length <= (f.maxLength ?? 500)
        })
  async function start() {
    if (!entry || !openingAvailable || !complete || starting || !playable)
      return
    const payload = {
      package: { package_id: packageId, version },
      entry_point_id: entry.id,
      identity_opening: true,
      ...(mode === 'source'
        ? { source_character_id: character }
        : { new_character: profile }),
    }
    const signature = JSON.stringify(payload)
    if (attempt.current?.signature !== signature)
      attempt.current = { signature, id: crypto.randomUUID() }
    setStarting(true)
    navigate('/sessions/new', {
      state: {
        openingKey: attempt.current!.id,
        opening: { request: { ...payload, request_id: attempt.current!.id }, catalog },
      },
    })
  }

  return (
    <main className="page adventure-setup">
      <button
        className="text-button back-link"
        disabled={starting}
        onClick={() => (step > 0 ? setStep(step - 1) : navigate('/packages'))}
      >
        {step === 1 ? '故事背景' : '书架'}
      </button>
      {step === 0 ? (
        <section className="prologue">
          <img src={storyImage(catalog.package.title)} alt="故事背景插画" />
          <div className="prologue-copy">
            <span className="eyebrow">序幕</span>
            <h1>{catalog.package.title}</h1>
            <div className="prologue-passage">
              {prologueText(catalog).split('\n').map((paragraph, i) => (
                <p key={i}>{paragraph}</p>
              ))}
            </div>
            <button className="button primary large" onClick={() => setStep(1)}>
              选择身份
            </button>
          </div>
        </section>
      ) : (
        <section className="identity-selection">
          <div className="quiet-heading">
            <div>
              <span className="eyebrow">{catalog.package.title}</span>
              <h1>请选择你的身份</h1>
            </div>
            {catalog.new_character?.enabled &&
              entry?.available_to_new_character && (
                <div className="segmented">
                  <button
                    aria-pressed={mode === 'source'}
                    disabled={starting}
                    onClick={() => setMode('source')}
                  >
                    故事人物
                  </button>
                  <button
                    aria-pressed={mode === 'new'}
                    disabled={starting}
                    onClick={() => setMode('new')}
                  >
                    自创身份
                  </button>
                </div>
              )}
          </div>
          {mode === 'source' ? (
            <>
              <div className="identity-categories" role="group" aria-label="身份分类">
                {groups.map((group) => (
                  <button
                    key={group.id}
                    className="identity-category"
                    aria-pressed={activeGroup?.id === group.id}
                    aria-controls="identity-members"
                    disabled={starting}
                    onClick={() => {
                      if (activeGroup?.id !== group.id) {
                        setSelectedGroup(group.id)
                        setCharacter(group.members[0]?.id ?? '')
                      }
                    }}
                  >
                    <span>{group.label}</span>
                    <small>
                      {group.members.length} 位可扮演
                    </small>
                  </button>
                ))}
              </div>
              <div id="identity-members" className="identity-deck" role="group" aria-label={activeGroup?.label}>
                <div className="identity-group">
                  {activeGroup?.members.map((c, i) => (
                    <button
                  key={c.id}
                  className={`identity-card ${character === c.id ? 'selected' : ''}`}
                  aria-pressed={character === c.id}
                  disabled={starting}
                  onClick={() => setCharacter(c.id)}
                >
                  {c.portraitAsset ? (
                    <img
                      className="identity-art character-portrait"
                      src={c.portraitAsset}
                      alt={`${c.name ?? '故事角色'}肖像`}
                    />
                  ) : <div
                    className={`identity-art ${catalog.package.title.includes('雨夜候车室') && ['许川', '唐栖', '陈砚', '姜序'].includes(c.name ?? '') ? 'rainy-portrait' : 'generic-portrait'}`}
                    style={{
                      backgroundPosition: `${(Math.max(0, ['许川', '唐栖', '陈砚', '姜序'].indexOf(c.name ?? '')) * 100) / 3}% top`,
                    }}
                  />}
                  <span className="identity-index" aria-hidden="true">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span className="identity-check" aria-hidden="true">
                    {character === c.id ? '✓' : '◇'}
                  </span>
                  <span className="identity-copy">
                    <strong>{c.name ?? '故事角色'}</strong>
                    <small>{c.identitySummary ?? identityText(catalog, c)}</small>
                    {c.openingHook && <em>{c.openingHook}</em>}
                  </span>
                    </button>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div className="profile-fields new-identity-form">
              {catalog.new_character?.profile_fields.map((f) => (
                <label key={f.id}>
                  {f.label}
                  <input
                    type={f.type === 'integer' ? 'number' : 'text'}
                    min={f.minimum}
                    max={f.maximum}
                    step={f.type === 'integer' ? 1 : undefined}
                    minLength={f.minLength}
                    maxLength={f.maxLength}
                    value={profile[f.id] ?? ''}
                    disabled={starting}
                    onChange={(e) =>
                      setProfile((p) => ({
                        ...p,
                        [f.id]:
                          f.type === 'integer' && e.target.value !== ''
                            ? Number(e.target.value)
                            : e.target.value,
                      }))
                    }
                  />
                </label>
              ))}
            </div>
          )}
          <div className="identity-start">
            <span>
              {mode === 'source'
                ? person?.name
                : profile.name
                  ? String(profile.name)
                  : ''}
            </span>
            <button
              className="button primary large"
              disabled={!complete || !openingAvailable || !playable || starting}
              onClick={start}
            >
              {starting ? '正在进入…' : '开始故事 '}
            </button>
          </div>
          {!playable && (
            <p className="muted">暂时无法开始新故事，请稍后重试。</p>
          )}
          {!entry && <p className="muted">这本书暂时没有可用的开篇。</p>}
        </section>
      )}
      {error && <Notice text={error} />}
    </main>
  )
}
