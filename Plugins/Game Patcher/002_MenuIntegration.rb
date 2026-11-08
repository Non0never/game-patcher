if defined?(UI::Load) && defined?(UI::LoadVisuals)
  class UI::Load
    alias game_patcher_initialize initialize
    def initialize(*args)
      GamePatcher.start_check
      game_patcher_initialize(*args)
    end

    ACTIONS.add(:game_patcher_update, {
      :effect => proc { |screen| GamePatcher.install(screen) }
    })
  end

  class UI::LoadVisuals
    alias game_patcher_update_visuals update_visuals
    def update_visuals
      game_patcher_update_visuals
      GamePatcher.notify(self)
    end
  end

  MenuHandlers.add(:load_screen, :game_patcher_update, {
    'name' => 'Update',
    'order' => 50
  })
elsif defined?(PokemonLoadScreen) && defined?(PokemonLoad_Scene)
  module GamePatcher::LegacyScene
    def pbStartScene(commands, *args)
      @game_patcher_ready = false
      @game_patcher_index = commands.length
      commands << 'Update'
      GamePatcher.start_check
      super
    end

    def pbStartScene2
      super
      @game_patcher_ready = true
    end

    def pbUpdate
      super
      GamePatcher.notify(self) if @game_patcher_ready
    end

    def pbChoose(commands)
      loop do
        command = super
        return command if command != @game_patcher_index
        pbPlayDecisionSE
        GamePatcher.install(GamePatcher::LegacyScreen.new(self))
      end
    end

    def pbEndScene
      @game_patcher_ready = false
      super
    end
  end

  class GamePatcher::LegacyScreen
    def initialize(scene)
      @scene = scene
    end

    def show_confirm_message(message)
      window = pbCreateMessageWindow
      choose = proc do |message_window|
        pbShowCommands(message_window, [_INTL('Yes'), _INTL('No')], 2, 0) { @scene.pbUpdate }
      end
      pbMessageDisplay(window, message, true, choose) { @scene.pbUpdate } == 0
    ensure
      pbDisposeMessageWindow(window) if window
      Input.update
    end

    def show_message(message)
      pbMessage(message) { @scene.pbUpdate }
    end

    def end_screen
      @scene.pbEndScene
    end
  end

  PokemonLoad_Scene.prepend(GamePatcher::LegacyScene)
else
  warn 'Game Patcher: unsupported load menu; menu integration was not loaded.'
end
